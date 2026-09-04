"""Rigorous out-of-sample ML validation.

W24-2 — Implements a formal out-of-sample validation pipeline that
guarantees NO look-ahead bias:

1. **Three-way time-ordered split** — Train (60%) → Validation (20%) →
   Out-of-sample Test (20%). Rows are sorted by timestamp ascending;
   no random shuffling is performed. This is the cardinal anti-leakage
   rule for any time-series model.

2. **Purge period** — A configurable gap (default 5 % of the data) is
   dropped between the train and validation windows. Without this, the
   tail of the training window could share overlapping label windows
   with the head of the validation window (e.g. both windows sample a
   24-hour-forward outcome from observations only a few minutes apart).
   Purging breaks that leakage path.

3. **Embargo period** — Same idea, between the validation and the
   out-of-sample test windows. Prevents the validation window's
   right-edge labels from leaking into the test window's left-edge
   predictions. This is the textbook "embargo" from López de Prado's
   *Advances in Financial Machine Learning* (Ch. 7, "Cross-Validation
   in Finance").

4. **Honest reporting** — Out-of-sample metrics (``test_auc`` /
   ``test_brier`` / ``test_calibration_error`` / ``oos_*`` P&L fields)
   are reported SEPARATELY from the in-sample metrics
   (``train_auc`` / ``train_brier``). The ``auc_decay`` /
   ``brier_increase`` fields quantify the generalization gap directly,
   and ``is_overfit`` / ``is_valid`` flag models that should not be
   promoted into production.

The module exposes:

* ``OutOfSampleResult`` — dataclass holding the full per-run metric
  payload (split info + in-sample / validation / out-of-sample metrics
  + overfitting diagnostics + simulated OOS P&L).
* ``OutOfSampleValidator`` — the validator class. Use
  ``validator.validate(model_factory, features, labels, timestamps)``
  for the full pipeline; ``validator.split(features, labels,
  timestamps)`` returns the raw splits for callers that want to fit /
  evaluate their own model.
* ``oos_validator`` — process-wide singleton (mirrors the
  ``drift_detector`` / ``calibrator`` / ``model_registry`` pattern).
* ``register_routes(app)`` — appends the ``POST /api/ml/out-of-sample``
  HTTP endpoint to a FastAPI app. Same additive registration pattern
  as ``ml.validation.register_routes`` / ``ml.explainability.register_routes``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

logger = logging.getLogger(__name__)


# ── Result container ───────────────────────────────────────────────────────


@dataclass
class OutOfSampleResult:
    """Full per-run out-of-sample validation payload.

    Field order is load-bearing — the ``validate()`` method unpacks the
    ``split_info`` dict via ``OutOfSampleResult(**split_info, ...)`` so
    every split-info key must be a dataclass field name. The
    ``timestamp`` field is the only one with a default (``time.time()``)
    so it must come last (Python dataclass rule: fields with defaults
    must follow fields without).
    """

    # ── Split info ───────────────────────────────────────────────────────
    train_size: int
    validation_size: int
    test_size: int
    purge_size: int
    embargo_size: int
    train_start: float
    train_end: float
    val_start: float
    val_end: float
    test_start: float
    test_end: float

    # ── In-sample metrics (train) ───────────────────────────────────────
    train_auc: float
    train_brier: float
    train_accuracy: float

    # ── Validation metrics ──────────────────────────────────────────────
    val_auc: float
    val_brier: float
    val_accuracy: float

    # ── Out-of-sample metrics (test — the honest numbers) ──────────────
    test_auc: float
    test_brier: float
    test_accuracy: float
    test_calibration_error: float

    # ── Overfitting detection ───────────────────────────────────────────
    auc_decay: float          # train_auc - test_auc (large positive = overfitting)
    brier_increase: float     # test_brier - train_brier (large positive = overfitting)

    # ── P&L simulation on out-of-sample ─────────────────────────────────
    oos_expectancy: float     # Expected P&L per trade on out-of-sample
    oos_win_rate: float
    oos_profit_factor: float
    oos_n_trades: int

    # ── Verdicts ────────────────────────────────────────────────────────
    is_overfit: bool          # True if auc_decay > 0.15 or brier_increase > 0.05
    is_valid: bool            # True if test_auc > 0.55 and not overfit

    timestamp: float = field(default_factory=time.time)


# ── Validator ────────────────────────────────────────────────────────────


class OutOfSampleValidator:
    """Rigorous out-of-sample validation with purge and embargo.

    The validator is stateless — every ``validate()`` call trains a
    fresh model (via the caller-supplied ``model_factory``) and emits a
    single :class:`OutOfSampleResult`. Safe to call concurrently from
    multiple threads / processes; the singleton ``oos_validator`` is
    only a convenience alias for a default-configured instance.
    """

    def __init__(self, purge_pct: float = 0.05, embargo_pct: float = 0.05):
        """
        Args:
            purge_pct:    Fraction of ``n`` to drop between the train and
                          validation windows (label-leakage guard).
            embargo_pct:  Fraction of ``n`` to drop between the validation
                          and out-of-sample test windows.
        """
        if not 0.0 <= purge_pct < 0.20:
            raise ValueError(
                f"purge_pct must be in [0.0, 0.20); got {purge_pct} "
                f"(>0.20 would consume >40 % of the data on purge + embargo alone)"
            )
        if not 0.0 <= embargo_pct < 0.20:
            raise ValueError(
                f"embargo_pct must be in [0.0, 0.20); got {embargo_pct}"
            )
        self.purge_pct = purge_pct
        self.embargo_pct = embargo_pct

    # ── Splitting ──────────────────────────────────────────────────────
    def split(
        self,
        features: np.ndarray,
        labels: np.ndarray,
        timestamps: np.ndarray,
    ) -> tuple[
        np.ndarray, np.ndarray,
        np.ndarray, np.ndarray,
        np.ndarray, np.ndarray,
        dict[str, Any],
    ]:
        """Create a three-way time-ordered split with purge + embargo.

        The split is **deterministic** given the inputs: rows are sorted
        by ``timestamps`` ascending (no random shuffling — the cardinal
        anti-leakage rule for time-series data), then partitioned into
        the 60 % / 20 % / 20 % train / val / test windows separated by
        the purge and embargo gaps.

        Args:
            features:   (N, F) feature matrix.
            labels:     (N,) binary labels in ``{0, 1}``.
            timestamps: (N,) float timestamps used for the time-ordering
                        sort. Need not be Unix epoch seconds — any
                        monotonic scalar works.

        Returns:
            ``(X_train, y_train, X_val, y_val, X_test, y_test, split_info)``.
            ``split_info`` carries the per-window sizes + boundary
            timestamps so a caller can persist them alongside the
            :class:`OutOfSampleResult` for audit-trail purposes.
        """
        n = len(features)

        # Sort by timestamp ascending (CRITICAL — no random shuffling).
        order = np.argsort(timestamps, kind="stable")
        features = np.asarray(features)[order]
        labels = np.asarray(labels)[order]
        timestamps = np.asarray(timestamps)[order]

        # ── Compute split points ───────────────────────────────────────
        purge_n = int(n * self.purge_pct)
        embargo_n = int(n * self.embargo_pct)

        train_end = int(n * 0.60)
        val_start = train_end + purge_n
        val_end = val_start + int(n * 0.20)
        test_start = val_end + embargo_n

        # ── Slice the three windows (the purge / embargo rows are dropped) ──
        X_train = features[:train_end]
        y_train = labels[:train_end]

        X_val = features[val_start:val_end]
        y_val = labels[val_start:val_end]

        X_test = features[test_start:]
        y_test = labels[test_start:]

        # ── Boundary timestamps (defensive — handle empty / degenerate windows) ──
        def _ts(arr: np.ndarray, idx: int) -> float:
            if arr is None or len(arr) == 0:
                return 0.0
            if idx < 0:
                idx = 0
            if idx >= len(arr):
                idx = len(arr) - 1
            return float(arr[idx])

        split_info: dict[str, Any] = {
            "train_size": int(len(X_train)),
            "validation_size": int(len(X_val)),
            "test_size": int(len(X_test)),
            "purge_size": int(purge_n),
            "embargo_size": int(embargo_n),
            "train_start": _ts(timestamps, 0),
            "train_end": _ts(timestamps, train_end - 1) if train_end > 0 else 0.0,
            "val_start": _ts(timestamps, val_start) if val_start < n else 0.0,
            "val_end": _ts(timestamps, val_end - 1) if 0 < val_end <= n else 0.0,
            "test_start": _ts(timestamps, test_start) if test_start < n else 0.0,
            "test_end": _ts(timestamps, n - 1) if n > 0 else 0.0,
        }

        logger.info(
            "OOS split: train=%d, val=%d, test=%d, purge=%d, embargo=%d",
            len(X_train), len(X_val), len(X_test), purge_n, embargo_n,
        )

        return X_train, y_train, X_val, y_val, X_test, y_test, split_info

    # ── Full validation pipeline ───────────────────────────────────────
    def validate(
        self,
        model_factory: Callable[[], Any],
        features: np.ndarray,
        labels: np.ndarray,
        timestamps: np.ndarray,
    ) -> OutOfSampleResult:
        """Run the full out-of-sample validation pipeline.

        Trains a fresh model on the train window, evaluates it on the
        validation and out-of-sample test windows, computes the
        overfitting diagnostics and the simulated OOS P&L, and returns
        the single :class:`OutOfSampleResult` payload.

        Args:
            model_factory: Zero-arg callable that returns a fresh, unfitted
                           sklearn-style classifier (``.fit(X, y)`` +
                           ``.predict_proba(X)``). Called exactly once per
                           ``validate()`` invocation.
            features:      (N, F) feature matrix.
            labels:        (N,) binary labels in ``{0, 1}``.
            timestamps:    (N,) float timestamps (any monotonic scalar).

        Returns:
            :class:`OutOfSampleResult`. When the data is too small for the
            minimum-size guard (``len(X_train) < 50`` / ``len(X_val) < 20``
            / ``len(X_test) < 20``) a zeroed :class:`OutOfSampleResult`
            with ``is_valid=False`` is returned (no exception raised —
            the caller can branch on ``is_valid``).
        """
        X_train, y_train, X_val, y_val, X_test, y_test, split_info = self.split(
            features, labels, timestamps,
        )

        # ── Minimum-size guard ──────────────────────────────────────────
        # Each window needs enough rows for the model to fit / the metrics
        # to be defined. Below these thresholds the result is structurally
        # meaningless — we return a zeroed envelope rather than raising so
        # an operator polling the endpoint during cold-start doesn't get a
        # 500.
        if len(X_train) < 50 or len(X_val) < 20 or len(X_test) < 20:
            logger.warning(
                "Insufficient data for OOS validation "
                "(train=%d val=%d test=%d — need >=50/20/20)",
                len(X_train), len(X_val), len(X_test),
            )
            return OutOfSampleResult(
                **split_info,
                train_auc=0.0, train_brier=0.0, train_accuracy=0.0,
                val_auc=0.0, val_brier=0.0, val_accuracy=0.0,
                test_auc=0.0, test_brier=0.0, test_accuracy=0.0,
                test_calibration_error=0.0,
                auc_decay=0.0, brier_increase=0.0,
                oos_expectancy=0.0, oos_win_rate=0.0,
                oos_profit_factor=0.0, oos_n_trades=0,
                is_overfit=False, is_valid=False,
            )

        # ── Train on the train window ONLY ──────────────────────────────
        model = model_factory()
        model.fit(X_train, y_train)

        # ── In-sample metrics (train) ───────────────────────────────────
        train_pred = self._predict_proba(model, X_train)
        train_auc = self._safe_auc(y_train, train_pred)
        train_brier = self._safe_brier(y_train, train_pred)
        train_acc = accuracy_score(y_train, (train_pred > 0.5).astype(int))

        # ── Validation metrics ──────────────────────────────────────────
        val_pred = self._predict_proba(model, X_val)
        val_auc = self._safe_auc(y_val, val_pred)
        val_brier = self._safe_brier(y_val, val_pred)
        val_acc = accuracy_score(y_val, (val_pred > 0.5).astype(int))

        # ── OUT-OF-SAMPLE metrics (test — the honest numbers) ───────────
        test_pred = self._predict_proba(model, X_test)
        test_auc = self._safe_auc(y_test, test_pred)
        test_brier = self._safe_brier(y_test, test_pred)
        test_acc = accuracy_score(y_test, (test_pred > 0.5).astype(int))
        test_ece = self._compute_ece(test_pred, y_test)

        # ── Overfitting detection ───────────────────────────────────────
        auc_decay = train_auc - test_auc
        brier_increase = test_brier - train_brier
        is_overfit = bool(auc_decay > 0.15 or brier_increase > 0.05)

        # ── P&L simulation on out-of-sample ──────────────────────────────
        oos_pnl, oos_wins, oos_losses = self._simulate_pnl(test_pred, y_test)
        oos_n = len(oos_pnl)
        oos_win_rate = (len(oos_wins) / oos_n) if oos_n > 0 else 0.0
        oos_expectancy = float(np.mean(oos_pnl)) if oos_pnl else 0.0
        gross_profit = float(sum(oos_wins)) if oos_wins else 0.0
        gross_loss = float(abs(sum(oos_losses))) if oos_losses else 0.0
        if gross_loss > 0.0:
            oos_pf = gross_profit / (gross_loss + 1e-8)
        elif gross_profit > 0.0:
            oos_pf = 999.0
        else:
            oos_pf = 0.0

        is_valid = bool(test_auc > 0.55 and not is_overfit)

        result = OutOfSampleResult(
            **split_info,
            train_auc=float(train_auc),
            train_brier=float(train_brier),
            train_accuracy=float(train_acc),
            val_auc=float(val_auc),
            val_brier=float(val_brier),
            val_accuracy=float(val_acc),
            test_auc=float(test_auc),
            test_brier=float(test_brier),
            test_accuracy=float(test_acc),
            test_calibration_error=float(test_ece),
            auc_decay=float(auc_decay),
            brier_increase=float(brier_increase),
            oos_expectancy=float(oos_expectancy),
            oos_win_rate=float(oos_win_rate),
            oos_profit_factor=float(oos_pf) if oos_pf < 999.0 else 999.0,
            oos_n_trades=int(oos_n),
            is_overfit=is_overfit,
            is_valid=is_valid,
        )

        logger.info(
            "OOS validation: test_auc=%.3f, test_brier=%.3f, "
            "auc_decay=%.3f, overfit=%s, valid=%s",
            test_auc, test_brier, auc_decay, is_overfit, is_valid,
        )

        return result

    # ── Helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _predict_proba(model: Any, X: np.ndarray) -> np.ndarray:
        """Return the positive-class probability vector from ``model``.

        Handles sklearn's 2-D ``predict_proba`` output (takes ``[:, 1]``)
        and 1-D regressor-style outputs that already emit probabilities
        directly. Mirrors the contract in
        ``ml.validation._predict_proba`` so any sklearn-style classifier
        works out of the box.
        """
        proba = model.predict_proba(X)
        arr = np.asarray(proba, dtype=np.float64)
        if arr.ndim == 2 and arr.shape[1] >= 2:
            return arr[:, 1]
        return arr.ravel()

    @staticmethod
    def _safe_auc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
        try:
            return float(roc_auc_score(y_true, y_pred))
        except Exception:  # noqa: BLE001 — AUC undefined for single-class y
            return 0.5

    @staticmethod
    def _safe_brier(y_true: np.ndarray, y_pred: np.ndarray) -> float:
        try:
            return float(brier_score_loss(y_true, y_pred))
        except Exception:  # noqa: BLE001 — Brier undefined for empty y
            return 0.25

    @staticmethod
    def _compute_ece(
        probs: np.ndarray, labels: np.ndarray, n_bins: int = 10,
    ) -> float:
        """Expected Calibration Error (ECE).

        Partitions the probability space into ``n_bins`` equal-width
        buckets; for each non-empty bucket, accumulates the bucket's
        sample-weighted absolute ``|confidence - accuracy|`` gap. ECE=0
        means perfectly calibrated (predicted probabilities match
        observed empirical frequencies in every bucket); ECE=1 means
        maximally mis-calibrated.
        """
        probs = np.asarray(probs, dtype=np.float64).ravel()
        labels = np.asarray(labels, dtype=np.float64).ravel()
        n = len(probs)
        if n == 0:
            return 0.0
        bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0
        for i in range(n_bins):
            if i == n_bins - 1:
                # Right-inclusive on the last bin so prob=1.0 lands in
                # the top bucket (consistent with np.histogram's rightmost
                # bin behaviour).
                mask = (probs >= bin_edges[i]) & (probs <= bin_edges[i + 1])
            else:
                mask = (probs >= bin_edges[i]) & (probs < bin_edges[i + 1])
            if mask.sum() == 0:
                continue
            bin_conf = float(probs[mask].mean())
            bin_acc = float(labels[mask].mean())
            ece += (mask.sum() / n) * abs(bin_conf - bin_acc)
        return float(ece)

    @staticmethod
    def _simulate_pnl(
        predictions: np.ndarray, labels: np.ndarray,
    ) -> tuple[list[float], list[float], list[float]]:
        """Simulate a flat-$1 per-trade P&L on the out-of-sample test set.

        Each prediction emits a binary bet: YES (``pred > 0.5``) or NO
        (``pred <= 0.5``). If the bet matches the actual label, +$1.0;
        else -$1.0. Returns ``(pnls, wins, losses)`` where ``wins`` and
        ``losses`` are the per-trade P&L sub-lists (used to compute the
        profit factor = ``sum(wins) / |sum(losses)|``).

        This is intentionally a flat-stake simulation — no Kelly sizing,
        no spread / fee modelling. The number's job is to give the
        operator a single signed scalar for the *direction* of the
        out-of-sample edge, not to predict live trading P&L.
        """
        pnls: list[float] = []
        wins: list[float] = []
        losses: list[float] = []
        for pred, actual in zip(predictions, labels):
            bet = 1 if pred > 0.5 else 0
            if bet == int(actual):
                pnl = 1.0
                wins.append(pnl)
            else:
                pnl = -1.0
                losses.append(pnl)
            pnls.append(pnl)
        return pnls, wins, losses


# ── Singleton ─────────────────────────────────────────────────────────────


oos_validator = OutOfSampleValidator()


# ── HTTP surface ──────────────────────────────────────────────────────────


def register_routes(app: Any) -> None:
    """Append ``POST /api/ml/out-of-sample`` to a FastAPI app.

    The endpoint pulls the current training data via
    ``ml_model.get_training_data()``, builds a fresh ensemble via
    ``ml_model._create_ensemble()``, and runs the
    :class:`OutOfSampleValidator` end-to-end. The returned
    :class:`OutOfSampleResult` is serialized via ``dataclasses.asdict``
    so the JSON response carries every per-window metric separately
    (no aggregation — the operator can read train vs. validation vs.
    test directly).

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing
    ``enforce_api_auth`` middleware (this path is NOT in
    ``PUBLIC_PATHS``). Same additive registration pattern as
    ``ml.validation.register_routes`` / ``ml.explainability.register_routes``.
    """
    from dataclasses import asdict as _asdict  # noqa: PLC0415 — local import keeps the module importable in non-server contexts
    from fastapi import HTTPException  # noqa: PLC0415 — FastAPI optional at module load

    @app.post(
        "/api/ml/out-of-sample",
        tags=["ml"],
        summary="Run rigorous out-of-sample ML validation",
        description=(
            "Three-way time-ordered split (train 60 % → validation 20 % "
            "→ out-of-sample test 20 %) with purge + embargo periods "
            "between the windows to prevent label leakage from "
            "overlapping forward-looking labels. Reports in-sample, "
            "validation, AND out-of-sample metrics separately so the "
            "operator can read the generalization gap directly. The "
            "``is_overfit`` / ``is_valid`` fields gate model promotion."
        ),
    )
    async def run_out_of_sample_validation():
        """Run the full out-of-sample validation pipeline."""
        from ml.model import ml_model
        from ml.out_of_sample import oos_validator

        try:
            features, labels, timestamps = ml_model.get_training_data()
        except Exception as e:  # noqa: BLE001 — defensive
            raise HTTPException(
                status_code=503,
                detail=(
                    "Training data unavailable — get_training_data() "
                    f"raised: {e!r}"
                ),
            )

        if features is None or len(features) < 100:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Insufficient training data for OOS validation "
                    f"(got {0 if features is None else len(features)} rows; "
                    "need >= 100 to satisfy the train>=50 / val>=20 / "
                    "test>=20 minimum-size guard after the purge + embargo "
                    "gaps)."
                ),
            )

        try:
            result = oos_validator.validate(
                model_factory=lambda: ml_model._create_ensemble(),
                features=features,
                labels=labels,
                timestamps=timestamps,
            )
        except Exception as e:  # noqa: BLE001 — defensive last net
            logger.error(
                "[oos_validator] /api/ml/out-of-sample failed: %s",
                e, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "OOS validation failed — see server logs for details "
                    "(correlate via the X-Request-ID response header)."
                ),
            )

        return _asdict(result)


__all__ = [
    "OutOfSampleResult",
    "OutOfSampleValidator",
    "oos_validator",
    "register_routes",
]
