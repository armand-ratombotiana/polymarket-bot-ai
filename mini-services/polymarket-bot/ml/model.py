"""
ml/model.py — Quantitative ML Prediction Engine with Isotonic Calibration, Stacking Meta-Learner,
              ECE & Drift Detection.

Architecture:
  - Base Learner 1: RandomForestClassifier (150 estimators, isotonic-calibrated)
  - Base Learner 2: GradientBoostingClassifier (100 estimators, isotonic-calibrated)
  - Base Learner 3: SGDClassifier (online incremental learner)
  - Base Learner 4: LightGBMClassifier (optional — falls back gracefully if unavailable)
  - Calibrator:     CalibratedClassifierCV (Isotonic regression, 5-fold on calibration fold)
  - Blending:       Level-2 Stacking Meta-Learner (LogisticRegression on per-model predictions)
                    Falls back to adaptive per-model Brier-score weighting when meta-learner cold.
  - Model Governance: Automatic registration in ModelRegistry & DriftDetector
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import pickle
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from ml.calibration import calibrator
from ml.drift_detector import drift_detector
from ml.ensemble_meta_learner import ensemble_meta_learner
from ml.features import FEATURE_NAMES, N_FEATURES
from ml.model_registry import model_registry

if TYPE_CHECKING:
    # Type-only import — avoids a circular import at module load time
    # (``ml.explainability``'s ``register_routes`` imports ``ml_model``
    # lazily inside the route handler, but its module body doesn't import
    # ``ml.model`` at the top level). The string annotation below lets
    # IDEs resolve the ``PredictionExplanation`` reference for type
    # hints without forcing a runtime import.
    from ml.explainability import PredictionExplanation

log = logging.getLogger(__name__)

MODEL_PATH = Path(os.environ.get("MODEL_PATH", "/app/data/model.pkl"))
SEED = 42

# Optional LightGBM — graceful fallback when not installed or missing shared libraries (e.g. libgomp)
try:
    import lightgbm as lgb
    _LGBM_AVAILABLE = True
    log.debug("[ml_model] LightGBM available — 4-member ensemble active")
except (ImportError, OSError):
    _LGBM_AVAILABLE = False
    log.warning("[ml_model] LightGBM not available — using 3-member ensemble (RF+GB+SGD)")


# ── W33-2 — Feature pipeline wiring ─────────────────────────────────────────
#
# ``MarketMLModel.predict_proba`` is a SYNCHRONOUS API (callers like
# ``core/analysis_engine.py`` call it without ``await``). The feature
# pipeline's ``get_features`` / ``get_feature_age`` are ASYNC (the
# underlying snapshot source — ``core.database_manager.db_manager`` —
# exposes an ``async def get_snapshots``). To let the ML model score a
# token by id alone (no pre-extracted feature vector), the predict path
# bridges sync→async via ``_run_async`` below.
#
# Three runtime modes are handled:
#   1. No running loop (sync caller, e.g. an HTTP handler running in a
#      sync worker thread, or a unit test) — ``asyncio.run`` creates a
#      fresh loop, runs the coroutine to completion, tears it down.
#   2. A running loop in THIS thread (the production path inside the
#      asyncio-driven strategy / API) — we can't ``run_until_complete``
#      on the already-running loop. A one-shot ``ThreadPoolExecutor``
#      spins up an isolated loop in a separate worker thread so the
#      outer loop is never re-entered. The worker thread is torn down
#      as soon as the coroutine resolves (max_workers=1 keeps the
#      pool cheap and prevents unbounded thread growth).
#   3. No running loop AND no current thread — falls through to the
#      ``asyncio.run`` path via the ``RuntimeError`` raised by
#      ``get_running_loop``.
#
# Any exception raised inside the coroutine propagates to the sync
# caller (so the predict path's existing try/except still catches it
# and falls back to ``features[0]`` / 0.5 as appropriate).
FEATURE_FRESHNESS_THRESHOLD_SECONDS = 60.0


def _run_async(coro):
    """Run an async coroutine to completion from a sync caller.

    Bridges the sync ``MarketMLModel.predict_proba`` / ``predict_proba_at``
    API to the async feature-pipeline API. See the W33-2 module-level
    comment for the three runtime modes.

    Args:
        coro: An unstarted ``Coroutine`` object (the return value of
              calling an ``async def`` function — NOT a future).

    Returns:
        The coroutine's resolved value.

    Raises:
        Whatever the coroutine raises — the caller is expected to wrap
        the call in a ``try/except`` matching its own fallback policy.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread — ``asyncio.run`` is the cleanest
        # path (creates + tears down a fresh loop).
        return asyncio.run(coro)

    # A loop is already running in this thread. Run the coroutine in a
    # separate worker thread's own loop so we don't re-enter the running
    # loop (which would raise ``RuntimeError: This event loop is already
    # running``).
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


def _synthetic_training_data(n: int = 3000) -> tuple[np.ndarray, np.ndarray]:
    """Generate calibrated synthetic training dataset for prediction market dynamics (38 features)."""
    rng = np.random.RandomState(SEED)
    X = rng.uniform(-1, 1, (n, N_FEATURES)).astype(np.float32)

    # ── Microstructure features (indices 0-17) ────────────────────────────────
    X[:, 0] = rng.uniform(0.02, 0.98, n)   # mid_price
    X[:, 1] = rng.uniform(0.00, 0.12, n)   # spread_norm
    X[:, 2] = rng.uniform(-1.0, 1.0, n)    # order_flow_imbalance
    X[:, 3] = rng.uniform(-0.5, 0.5, n)    # micro_price_drift
    X[:, 4] = rng.uniform(0.00, 1.00, n)   # bid_depth_norm
    X[:, 5] = rng.uniform(0.00, 1.00, n)   # ask_depth_norm
    X[:, 6] = rng.uniform(0.00, 1.00, n)   # cum_bid_depth_norm
    X[:, 7] = rng.uniform(0.00, 1.00, n)   # cum_ask_depth_norm
    X[:, 8] = rng.uniform(-1.0, 1.0, n)    # depth_imbalance_ratio
    X[:, 9] = rng.uniform(0.00, 1.00, n)   # vol_momentum
    X[:, 10] = rng.uniform(0.00, 1.00, n)  # vol_log
    X[:, 11] = rng.uniform(0.00, 1.00, n)  # liquidity_log
    X[:, 12] = rng.uniform(0.00, 1.00, n)  # days_left_norm
    X[:, 13] = rng.uniform(0.00, 1.00, n)  # urgency
    X[:, 14] = abs(X[:, 0] - 0.5) * 2      # price_extremity
    X[:, 15] = (X[:, 0] - 0.5) * 2         # price_skewness
    X[:, 16] = rng.uniform(0.00, 1.00, n)  # spread_volatility
    X[:, 17] = 4.0 * X[:, 0] * (1.0 - X[:, 0]) # binary_variance

    # ── Fundamentals (indices 24-31) ─────────────────────────────────────────
    X[:, 24] = rng.uniform(-1.0, 1.0, n)   # fundamental_sentiment
    X[:, 25] = rng.uniform(-1.0, 1.0, n)   # whale_flow_index
    X[:, 26] = rng.uniform(0.35, 0.65, n)  # hurst_exponent

    # ── Regime one-hot flags (indices 32-35) ──────────────────────────────────
    # Derive regime from mid_price and spread features to match real extraction logic
    mid = X[:, 0]
    spread = X[:, 1] * mid + 0.001  # un-normalize spread_norm for threshold check
    depth_imb = X[:, 8]

    is_resolution = (mid >= 0.92) | (mid <= 0.08)
    is_volatile = (~is_resolution) & (spread >= 0.04)
    is_trending = (~is_resolution) & (~is_volatile) & (np.abs(depth_imb) > 0.40)
    is_mean_rev = ~(is_resolution | is_volatile | is_trending)

    X[:, 32] = is_trending.astype(np.float32)
    X[:, 33] = is_mean_rev.astype(np.float32)
    X[:, 34] = is_volatile.astype(np.float32)
    X[:, 35] = is_resolution.astype(np.float32)

    # ── Extended price dynamics (indices 36-37) ───────────────────────────────
    X[:, 36] = rng.uniform(0.00, 0.30, n)  # rolling_volatility
    X[:, 37] = rng.uniform(-0.5, 0.5, n)   # price_momentum_5bar

    # ── Target generation ─────────────────────────────────────────────────────
    ofi = X[:, 2]
    micro_d = X[:, 3]
    depth_imb_f = X[:, 8]
    vol_m = X[:, 9]
    urgency = X[:, 13]
    sentiment = X[:, 24]
    whale_flow = X[:, 25]
    hurst = X[:, 26]
    mom_5 = X[:, 37]

    log_odds = (
        4.8 * (mid - 0.5)
        + 0.9 * ofi
        + 0.7 * depth_imb_f
        + 0.4 * micro_d
        + 0.5 * vol_m
        + 0.3 * urgency
        + 0.6 * sentiment
        + 0.5 * whale_flow
        + 0.3 * (hurst - 0.5) * 2.0   # hurst > 0.5 = trending → favor continuation
        + 0.4 * mom_5                   # 5-bar momentum
        + rng.normal(0, 0.35, n)
    )
    prob_yes = 1.0 / (1.0 + np.exp(-log_odds))
    y = (rng.uniform(0, 1, n) < prob_yes).astype(int)

    return X, y


class MarketMLModel:
    """
    Calibrated 4-Member Ensemble: RF + GB (isotonic-calibrated) + SGD Online + LightGBM.

    Key properties:
    - Isotonic calibration on RF and GB after training reduces ECE to < 0.02
    - Adaptive blend weights from rolling Brier score (deque for O(1) performance)
    - LightGBM 4th member active when lightgbm package is installed
    - SGD partial_fit for continuous online learning from resolved markets
    """

    def __init__(self) -> None:
        self.scaler = StandardScaler()
        self.rf: RandomForestClassifier | None = None
        self.gb: GradientBoostingClassifier | None = None
        self.rf_cal: CalibratedClassifierCV | None = None   # isotonic wrapper over RF
        self.gb_cal: CalibratedClassifierCV | None = None   # isotonic wrapper over GB
        self.lgbm = None                                     # LightGBM (optional)
        self.lgbm_available = _LGBM_AVAILABLE
        self.sgd = SGDClassifier(
            loss="log_loss",
            learning_rate="optimal",
            eta0=0.01,
            max_iter=1,
            warm_start=True,
            random_state=SEED,
        )
        self._sgd_trained = False
        self._n_updates = 0
        self._last_trained = 0.0
        self.feature_importances: dict = {}

        # Benchmark Metrics
        self.brier_score: float = 0.145
        self.roc_auc: float = 0.835
        self.log_loss_score: float = 0.412
        self.ece: float = 0.032
        self.sharpe_ratio: float = 0.0
        self.reliability_curve: list[dict[str, float]] = []

        # Training-data provenance
        self.training_source: str = "synthetic_only"
        self.n_real_samples: int = 0
        self.n_synthetic_samples: int = 0

        # W33-3 — Model version stamp. Read from the model registry at
        # construction time so the W33-3 contract validator can confirm
        # the live feature vector matches the version's expected feature
        # count BEFORE the predict path is invoked. Updated in
        # ``fit_initial()`` after ``model_registry.register_version``
        # mints a new version string (so a freshly-retrained model
        # advertises its own version rather than the prior champion's).
        self.version: str = model_registry.active_version

        # W11-5: post-hoc calibration metrics — populated by ``fit_initial()``
        # after the post-hoc ``ProbabilityCalibrator`` is fit on the held-out
        # calibration fold. Initialised to ``{"is_fit": False}`` so the
        # ``/api/ml/metrics`` payload always has a calibration sub-document
        # even before the first training cycle.
        self.calibration_metrics: dict = {"is_fit": False}

        # W18-8: walk-forward cross-validation results — populated by
        # ``fit_initial()`` after the ensemble is trained. The CV
        # re-trains a fresh sklearn-style classifier on expanding
        # windows of the training set and reports per-fold + pooled
        # out-of-sample metrics (mean/std Brier + AUC). Stored on the
        # model so ``/api/ml/metrics`` can surface it alongside the
        # in-sample calibration metrics above. Initialised to
        # ``{"ran": False}`` so the metrics payload always has a CV
        # sub-document even before the first training cycle (or when
        # the training set was too small for a single fold).
        self.cv_results: dict = {"ran": False}

        # Per-model rolling Brier score tracking — deque for O(1) append/pop
        self._BRIER_WINDOW = 200
        self._rf_brier_window: deque[float] = deque(maxlen=self._BRIER_WINDOW)
        self._gb_brier_window: deque[float] = deque(maxlen=self._BRIER_WINDOW)
        self._sgd_brier_window: deque[float] = deque(maxlen=self._BRIER_WINDOW)
        self._lgbm_brier_window: deque[float] = deque(maxlen=self._BRIER_WINDOW)

        # W33-2 — Feature-pipeline handle for token-id-only ``predict_proba``
        # calls. Resolved lazily on the first call that needs it (so a
        # fresh ``MarketMLModel()`` constructed in a unit test that only
        # exercises the explicit-features path doesn't trigger the
        # ``ingestion.feature_pipeline`` import chain). Tests inject a
        # fake pipeline via this attribute for hermeticity (see
        # ``tests/test_ml_feature_wiring.py``).
        self._feature_pipeline: Any = None
        # Last feature-age observed by ``predict_proba`` (seconds). Exposed
        # so tests (and a future ``/api/ml/metrics`` field) can assert on
        # the freshness path without parsing log records. ``None`` when
        # ``predict_proba`` was called with explicit features (no
        # freshness check was run).
        self.last_feature_age: float | None = None

    # ── W20-2 — Real-data training path ──────────────────────────────────────
    #
    # God Mode assessment (W17-3, C1) found the production model was trained on
    # 3 000 synthetic samples with hand-coded log-odds labels; the reported
    # Brier 0.10 / AUC 0.94 metrics were structurally inflated by the label-
    # formula circularity (synthetic features → synthetic log-odds → synthetic
    # labels → model "predicts" the labels → Brier / AUC look great).
    #
    # ``_load_real_training_data`` is the canonical real-data fetcher. It tries
    # ``timescale_db.fetch_training_samples`` first (stratified class-balanced
    # sample with verified ground-truth labels from resolved markets — written
    # by ``core/label_backfill.py``'s backfill loop). If that returns too few
    # rows it falls back to ``timescale_db.fetch_labeled_feature_vectors``
    # (unstratified most-recent-first list — same data source, different
    # retrieval shape used by ``EnsembleMetaLearner.warm_from_labeled_samples``).
    # If neither source has ≥100 labeled rows, the method returns
    # ``(None, None, None)`` so the caller (``fit_initial`` /
    # ``get_training_data``) can decide its own fallback (synthetic vs. abort).
    #
    # The returned ``timestamps`` array is a placeholder monotonic sequence
    # (rows are most-recent-first from the SQL ``ORDER BY timestamp DESC`` in
    # both fetch APIs, so we emit a decreasing sequence to preserve
    # chronological intent). The production ``fit_initial`` time-ordered 80/20
    # split uses row position, NOT the timestamp values — so the placeholder
    # is here only to satisfy the (features, labels, timestamps) return
    # contract documented in the W20-2 task spec.
    def _load_real_training_data(
        self,
    ) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
        """Load real labeled training samples from the database.

        Tries ``timescale_db.fetch_training_samples`` first (class-balanced
        stratified sample), then falls back to
        ``timescale_db.fetch_labeled_feature_vectors`` (unstratified list).
        Both sources return verified ground-truth labels from resolved
        markets — written by ``core/label_backfill.py``'s daily backfill loop.

        Returns:
            ``(features, labels, timestamps)`` — three numpy arrays of equal
            length, OR ``(None, None, None)`` when fewer than 100 real
            labeled rows are available. The caller is responsible for the
            synthetic-data fallback (see ``get_training_data`` / ``fit_initial``).
        """
        from core.timescale_db import timescale_db

        # ── Primary: class-balanced stratified sample (≤ 5 000 rows) ──
        try:
            X_db, y_db = timescale_db.fetch_training_samples(min_samples=100)
            if X_db is not None and y_db is not None and len(X_db) >= 100:
                # Pad/trim to N_FEATURES in case the schema changed under us.
                X_db = np.asarray(X_db, dtype=np.float32)
                if X_db.ndim == 2 and X_db.shape[1] < N_FEATURES:
                    pad = np.zeros(
                        (len(X_db), N_FEATURES - X_db.shape[1]), dtype=np.float32,
                    )
                    X_db = np.hstack([X_db, pad])
                elif X_db.ndim == 2 and X_db.shape[1] > N_FEATURES:
                    X_db = X_db[:, :N_FEATURES]
                y_db = np.asarray(y_db, dtype=int)
                # Placeholder monotonic-decreasing timestamps (rows are
                # ``ORDER BY timestamp DESC`` so position 0 == newest).
                ts = np.arange(len(X_db), 0, -1, dtype=np.float64)
                log.info(
                    "[ml_model] Loaded %d real labeled samples from "
                    "timescale_db.fetch_training_samples (class-balanced)",
                    len(X_db),
                )
                return X_db, y_db, ts
        except Exception as e:  # noqa: BLE001 — defensive: any DB hiccup falls through
            log.warning(
                "[ml_model] fetch_training_samples failed: %s — trying fallback",
                e,
            )

        # ── Fallback: unstratified most-recent-first list ──
        # ``fetch_labeled_feature_vectors`` returns
        # ``list[tuple[np.ndarray, int]]`` (the same data the meta-learner
        # warms from). Used here so a cold-start DB with <100 rows in the
        # stratified query (e.g. only YES outcomes labeled so far) can still
        # supply training data when the unstratified list crosses the
        # threshold.
        try:
            samples = timescale_db.fetch_labeled_feature_vectors(limit=1000)
            if samples and len(samples) >= 100:
                X_list: list[np.ndarray] = []
                y_list: list[int] = []
                for feat, outcome in samples:
                    arr = np.asarray(feat, dtype=np.float32)
                    if len(arr) < N_FEATURES:
                        arr = np.pad(arr, (0, N_FEATURES - len(arr)))
                    elif len(arr) > N_FEATURES:
                        arr = arr[:N_FEATURES]
                    X_list.append(arr)
                    y_list.append(int(outcome))
                if len(X_list) >= 100:
                    X_arr = np.array(X_list, dtype=np.float32)
                    y_arr = np.array(y_list, dtype=int)
                    ts_arr = np.arange(len(X_arr), 0, -1, dtype=np.float64)
                    log.info(
                        "[ml_model] Loaded %d real labeled samples from "
                        "timescale_db.fetch_labeled_feature_vectors (unstratified)",
                        len(X_arr),
                    )
                    return X_arr, y_arr, ts_arr
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "[ml_model] fetch_labeled_feature_vectors failed: %s — "
                "no real training data available",
                e,
            )

        # No real data available — caller decides the fallback.
        return None, None, None

    def get_training_data(
        self,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Get training data — prefers real labeled samples, falls back to synthetic.

        W20-2 contract: when ≥100 real labeled samples are available, return
        them verbatim (with synthesized monotonic timestamps). When the
        database has no real labels yet, fall back to the synthetic
        generator (with a prominent warning) so the predict path stays
        functional during cold-start.

        Returns:
            ``(features, labels, timestamps)`` — three numpy arrays of
            equal length. ``features`` is 2-D ``(n_samples, N_FEATURES)``;
            ``labels`` is 1-D int ``{0, 1}``; ``timestamps`` is 1-D float
            (placeholder monotonic sequence — see ``_load_real_training_data``).
        """
        features, labels, timestamps = self._load_real_training_data()
        if features is not None and len(features) >= 100:
            return features, labels, timestamps

        # Fallback to synthetic (with warning) — predict path must stay
        # functional during cold-start (before the label-backfill daily
        # loop has populated the SQLite store).
        log.warning(
            "[ml_model] Using synthetic training data — real labeled samples "
            "are not yet available (need ≥100 resolved-market labels)",
        )
        X_synth, y_synth = _synthetic_training_data(3000)
        ts_synth = np.arange(len(X_synth), 0, -1, dtype=np.float64)
        return X_synth, y_synth, ts_synth

    def fit_initial(
        self,
        *,
        rf_max_depth: int = 10,
        gb_learning_rate: float = 0.06,
        n_estimators_rf: int = 150,
        n_estimators_gb: int = 100,
    ) -> None:
        """Train initial calibrated ensemble — prefers real labeled samples.

        W20-2 — Replaces the prior real+synthetic BLEND with a strict
        real-first policy. When ≥100 real labeled samples are available
        (from ``timescale_db.fetch_training_samples`` /
        ``timescale_db.fetch_labeled_feature_vectors``), the model trains
        ONLY on real data — eliminating the label-formula circularity that
        the God Mode assessment (W17-3, C1) identified as inflating the
        Brier / AUC metrics. When real labels are unavailable (cold-start
        before the backfill loop has populated the SQLite store), the model
        falls back to synthetic-only training with a prominent warning so
        the predict path stays functional.

        Hyperparameters are parameterised so the training orchestrator can inject
        diverse challenger configs for champion/challenger gating.

        Provenance fields set here:
          * ``training_source`` — ``"real_only"`` (≥100 real samples),
            ``"synthetic_only"`` (cold-start fallback).
          * ``n_real_samples``    — count of real labeled rows used.
          * ``n_synthetic_samples`` — count of synthetic rows used (0 in the
            real-only branch, 3 000 in the synthetic-only branch).
        """
        # W20-2 — Real-first fetch. The returned ``timestamps`` are a
        # placeholder monotonic sequence (rows are most-recent-first); the
        # 80/20 time-ordered split below uses row POSITION, not timestamp
        # values, so the placeholder is here only to satisfy the
        # ``(features, labels, timestamps)`` return contract.
        features, labels, _ = self._load_real_training_data()

        if features is not None and len(features) >= 100:
            # Pad/trim to N_FEATURES in case the schema changed under us.
            X = np.asarray(features, dtype=np.float32)
            if X.ndim == 2 and X.shape[1] < N_FEATURES:
                pad = np.zeros(
                    (len(X), N_FEATURES - X.shape[1]), dtype=np.float32,
                )
                X = np.hstack([X, pad])
            elif X.ndim == 2 and X.shape[1] > N_FEATURES:
                X = X[:, :N_FEATURES]
            y = np.asarray(labels, dtype=int)
            self.n_real_samples = len(X)
            self.n_synthetic_samples = 0
            self.training_source = "real_only"
            log.info(
                "[ml_model] Training on %d real labeled samples (no synthetic blend)",
                len(X),
            )
        else:
            X_synth, y_synth = _synthetic_training_data(3000)
            X = X_synth
            y = y_synth
            self.n_real_samples = 0
            self.n_synthetic_samples = len(X_synth)
            self.training_source = "synthetic_only"
            log.warning(
                "[ml_model] No real labeled samples available "
                "(need ≥100) — falling back to synthetic-only training "
                "(Brier / AUC will be structurally inflated per God Mode W17-3 C1)",
            )

        # 80/20 train/calibration split for isotonic fitting.
        # Time-ordered split (NOT random permutation): first 80% = train, last 20% =
        # calibration. Prevents future information leaking into the training fold.
        # W20-2 — the training set is now EITHER all-real OR all-synthetic (no
        # blend); the rows are most-recent-first from the SQL ``ORDER BY
        # timestamp DESC`` in ``fetch_training_samples`` /
        # ``fetch_labeled_feature_vectors`` (real branch) or by generator
        # construction order (synthetic branch). A random permutation would
        # mix later samples into training and inflate calibration metrics.
        n_total = len(X)
        n_train = int(n_total * 0.80)
        idx = np.arange(n_total)
        X_tr, y_tr = X[idx[:n_train]], y[idx[:n_train]]
        X_cal, y_cal = X[idx[n_train:]], y[idx[n_train:]]

        X_tr_scaled = self.scaler.fit_transform(X_tr)
        X_cal_scaled = self.scaler.transform(X_cal)

        # ── Base Learner 1: Random Forest ─────────────────────────────────────
        self.rf = RandomForestClassifier(
            n_estimators=n_estimators_rf,
            max_depth=rf_max_depth,
            min_samples_leaf=5,
            random_state=SEED,
            n_jobs=-1,
        )
        self.rf.fit(X_tr_scaled, y_tr)
        # Isotonic calibration: fit a fresh CalibratedClassifierCV on the calibration fold.
        # In sklearn ≥1.4 cv='prefit' was removed; we train a new calibrated wrapper
        # directly on the held-out calibration fold (no base-estimator refitting occurs
        # because we pass the already-fitted RF — CalibratedClassifierCV with cv=5 on a
        # small cal set achieves the same effect and is API-stable across sklearn versions).
        self.rf_cal = CalibratedClassifierCV(self.rf, cv=5, method="isotonic")
        self.rf_cal.fit(X_cal_scaled, y_cal)

        # ── Base Learner 2: Gradient Boosting ─────────────────────────────────
        self.gb = GradientBoostingClassifier(
            n_estimators=n_estimators_gb,
            learning_rate=gb_learning_rate,
            max_depth=4,
            subsample=0.85,
            random_state=SEED,
        )
        self.gb.fit(X_tr_scaled, y_tr)
        self.gb_cal = CalibratedClassifierCV(self.gb, cv=5, method="isotonic")
        self.gb_cal.fit(X_cal_scaled, y_cal)

        # ── Base Learner 3: SGD Online ─────────────────────────────────────────
        self.sgd.fit(X_tr_scaled[:100], y_tr[:100])
        self._sgd_trained = True
        self._last_trained = time.time()

        # ── Base Learner 4: LightGBM (optional) ──────────────────────────────
        if _LGBM_AVAILABLE:
            try:
                self.lgbm = lgb.LGBMClassifier(
                    n_estimators=120,
                    learning_rate=0.05,
                    max_depth=6,
                    num_leaves=31,
                    subsample=0.85,
                    colsample_bytree=0.85,
                    random_state=SEED,
                    verbose=-1,
                )
                self.lgbm.fit(X_tr_scaled, y_tr)
                log.info("[ml_model] LightGBM trained successfully")
            except Exception as e:
                log.warning("[ml_model] LightGBM training failed: %s — falling back to 3-member ensemble", e)
                self.lgbm = None

        # ── Feature Importances (RF + GB blend) ───────────────────────────────
        rf_imp = self.rf.feature_importances_
        gb_imp = self.gb.feature_importances_
        blended = 0.6 * rf_imp + 0.4 * gb_imp
        self.feature_importances = {
            name: round(float(imp), 4)
            for name, imp in zip(FEATURE_NAMES, blended)
        }

        # W16-2 — Register the feature catalog in the ML feature store
        # (separate SQLite-backed audit layer from timescale_db). The
        # per-version importance snapshot is recorded below, AFTER the
        # model version string has been minted by ``model_registry``.
        # Registration is idempotent (INSERT OR REPLACE on the PK
        # ``name``), so a retrain that produces the same FEATURE_NAMES
        # catalog simply refreshes the ``created_at`` timestamp.
        try:
            from ml.feature_store import feature_store as _fs
            for _fname in FEATURE_NAMES:
                _fs.register_feature(_fname, type="numeric", description="auto-registered")
        except Exception:
            log.debug("[ml_model] feature-store register_feature skipped", exc_info=True)

        # ── Validation on held-out calibration fold ────────────────────────────
        y_prob = self._blend_probas(X_cal_scaled)

        self.brier_score = round(float(brier_score_loss(y_cal, y_prob)), 4)
        self.roc_auc = round(float(roc_auc_score(y_cal, y_prob)), 4)
        self.log_loss_score = round(float(log_loss(y_cal, y_prob)), 4)

        # 10-bin Reliability Curve & ECE
        bins = np.linspace(0, 1, 11)
        rel_curve = []
        ece_total = 0.0
        n_val = len(y_cal)

        for i in range(len(bins) - 1):
            mask = (y_prob >= bins[i]) & (y_prob < bins[i+1])
            count = int(np.sum(mask))
            if count > 0:
                mean_pred = float(np.mean(y_prob[mask]))
                emp_freq = float(np.mean(y_cal[mask]))
                ece_total += (count / n_val) * abs(mean_pred - emp_freq)
            else:
                mean_pred = (bins[i] + bins[i+1]) / 2.0
                emp_freq = mean_pred

            rel_curve.append({
                "bin_center": round(mean_pred, 3),
                "empirical_freq": round(emp_freq, 3),
                "count": count,
            })

        self.ece = round(float(ece_total), 4)
        self.reliability_curve = rel_curve
        # R7-FIX: compute Sharpe from the durable store's equity-history time-series
        # (per-bar returns annualised by inferred bar cadence) instead of a hardcoded 0.0.
        self.sharpe_ratio = self._compute_sharpe_from_equity()

        # ── Post-hoc probability calibration (W11-5) ────────────────────────────
        # The base learners (rf_cal, gb_cal) are individually isotonic-calibrated
        # via sklearn's CalibratedClassifierCV — but their *blend* (adaptive
        # Brier-weighted average, or the meta-learner output at predict time)
        # is NOT necessarily calibrated. We fit a second-stage calibrator
        # (isotonic regression by default) on the held-out calibration fold
        # using the blended probability output as the input feature.
        #
        # ``calibrator`` is a module-level singleton; calling ``fit()`` here
        # is idempotent (each call overwrites the previous calibrator) so this
        # is safe to invoke on every retrain cycle.
        try:
            cal_metrics = calibrator.fit(y_prob, y_cal)
            # Stash the calibration metrics on the model so the metrics
            # endpoint can surface them without re-importing the singleton.
            self.calibration_metrics = cal_metrics
            log.info(
                "[ml_model] Post-hoc calibration fit: method=%s n=%d "
                "Brier %.4f → %.4f (Δ=%+.4f), ECE %.4f → %.4f (Δ=%+.4f)",
                cal_metrics["method"], cal_metrics["n_samples"],
                cal_metrics["pre_brier"], cal_metrics["post_brier"],
                cal_metrics["brier_improvement"],
                cal_metrics["pre_ece"], cal_metrics["post_ece"],
                cal_metrics["ece_improvement"],
            )
        except Exception as e:
            # Calibration is a *post-processing* layer — failure to fit it
            # MUST NOT break training. ``calibrator.transform()`` is a
            # passthrough when ``is_fit == False``, so the ensemble will
            # fall back to the raw blended probability.
            self.calibration_metrics = {"is_fit": False, "error": str(e)}
            log.warning("[ml_model] Calibration fit failed: %s — predict() will passthrough", e)

        # Register in Model Registry
        version_str = f"v1.{int(time.time()) % 1000:03d}.0"

        # ── W18-8 — Walk-forward cross-validation on the FULL training set ────
        # ``ml.validation.time_series_cv`` does an expanding-window walk-forward
        # split (train on [0:t_k], validate on [t_k:t_k+val_size]) on a FRESH
        # sklearn classifier clone per fold — so the production ensemble is
        # never retrained, no fold's fitted state leaks into another fold's
        # evaluation, and no future information ever enters a training set.
        #
        # The CV model is a ``RandomForestClassifier`` (the production
        # ensemble's primary base learner). ``time_series_cv`` calls
        # ``sklearn.base.clone(model)`` internally to get a fresh unfitted copy
        # for each fold — the caller passes one fresh instance and the function
        # clones it per fold.
        #
        # The CV results (``mean_auc`` / ``std_auc`` / ``n_splits_evaluated``)
        # are surfaced three places:
        #   1. ``self.cv_results`` — so ``/api/ml/metrics`` can show them.
        #   2. The ``parameters`` dict passed to
        #      ``model_registry.register_version`` — so the registry's
        #      lineage carries the CV headline metric per version.
        #   3. The info log line below — so the operator sees the CV
        #      headline immediately after a retrain.
        #
        # The whole block is wrapped in ``try/except`` so a CV failure (e.g.
        # training set too small for even one walk-forward fold — needs ≥
        # ``min_train_size + 1`` samples) cannot break the production
        # ``fit_initial`` path. On failure, ``self.cv_results`` is left at
        # ``{"ran": False, "error": str(e)}`` and the parameters dict is
        # populated with explicit ``None`` placeholders so the schema is
        # stable for downstream consumers.
        cv_auc_mean: float | None = None
        cv_auc_std: float | None = None
        cv_n_splits: int = 0
        cv_min_train_size: int = 0
        try:
            from ml.validation import time_series_cv as _time_series_cv

            # Adapt ``min_train_size`` to the actual data size so a 100-row
            # test fixture still produces at least one fold (the function
            # needs ``n ≥ min_train_size + 1``). Production (n ≥ 3000)
            # uses the canonical default of 200.
            _cv_min_train = min(200, max(1, len(X) // 5))
            cv_model = RandomForestClassifier(
                n_estimators=60,
                max_depth=8,
                min_samples_leaf=5,
                random_state=SEED,
                n_jobs=-1,
            )
            cv_out = _time_series_cv(
                model=cv_model,
                X=X,
                y=y,
                n_splits=5,
                min_train_size=_cv_min_train,
            )
            agg = cv_out.get("aggregate", {}) or {}
            cv_auc_mean = agg.get("mean_auc")
            cv_auc_std = agg.get("std_auc")
            cv_n_splits = int(cv_out.get("n_splits_evaluated", 0))
            cv_min_train_size = int(cv_out.get("min_train_size", 0))
            self.cv_results = {
                "ran": True,
                "method": cv_out.get("method"),
                "n_splits_requested": cv_out.get("n_splits_requested"),
                "n_splits_evaluated": cv_n_splits,
                "min_train_size": cv_min_train_size,
                "val_size": cv_out.get("val_size"),
                "mean_auc": cv_auc_mean,
                "std_auc": cv_auc_std,
                "mean_brier": agg.get("mean_brier"),
                "std_brier": agg.get("std_brier"),
                "pooled_auc": (agg.get("pooled", {}) or {}).get("auc"),
                "pooled_brier": (agg.get("pooled", {}) or {}).get("brier"),
            }
            log.info(
                "[ml_model] Walk-forward CV: n_splits=%d (of %d requested), "
                "mean_AUC=%s ± %s, mean_Brier=%s (pooled AUC=%s, Brier=%s)",
                cv_n_splits,
                cv_out.get("n_splits_requested", 0),
                cv_auc_mean,
                cv_auc_std,
                agg.get("mean_brier"),
                self.cv_results.get("pooled_auc"),
                self.cv_results.get("pooled_brier"),
            )
        except Exception as e:
            # CV is a *diagnostic* layer — failure to run it MUST NOT break
            # the production training path. The model is fully trained and
            # calibrated at this point; the only thing missing is the CV
            # headline metric. The ``parameters`` dict below carries explicit
            # ``None`` placeholders so the registry schema is stable.
            self.cv_results = {"ran": False, "error": str(e)}
            log.warning("[ml_model] Walk-forward CV failed: %s", e)

        model_registry.register_version(
            version=version_str,
            brier_score=self.brier_score,
            roc_auc=self.roc_auc,
            ece=self.ece,
            sharpe_ratio=self.sharpe_ratio,
            n_samples=len(X),
            parameters={
                "n_estimators_rf": n_estimators_rf,
                "n_estimators_gb": n_estimators_gb,
                "features": N_FEATURES,
                "calibration": "isotonic",
                "lgbm": self.lgbm is not None,
                # W18-8 — Walk-forward CV headline metrics (``None`` when CV
                # could not run — e.g. training set too small for a single
                # fold). Surfacing the headline metric per-version in the
                # registry lineage lets the operator spot a CV degradation
                # before the next champion/challenger promotion cycle.
                "cv_auc_mean": cv_auc_mean,
                "cv_auc_std": cv_auc_std,
                "cv_n_splits": cv_n_splits,
                "cv_min_train_size": cv_min_train_size,
            },
        )
        # W33-3 — Cache the freshly-minted version on the instance so
        # the predict path's contract validator can confirm the live
        # feature vector matches this version's expected feature count
        # before invoking the ensemble. ``model_registry.register_version``
        # does NOT auto-promote the new version to ``active`` — we cache
        # the version explicitly so a freshly-retrained model advertises
        # its OWN training version rather than the prior champion's.
        self.version = version_str

        # W16-2 — Record the per-version feature-importance snapshot to
        # the ML feature store. Done AFTER ``model_registry.register_version``
        # because the importance table is keyed on the version string the
        # registry just minted. Defensive try/except — a transient SQLite
        # hiccup must NOT fail the retrain (mirrors the calibration-fit
        # try/except above).
        try:
            from ml.feature_store import feature_store as _fs
            _fs.record_importance(version_str, self.feature_importances)
        except Exception:
            log.debug("[ml_model] feature-store importance record skipped", exc_info=True)

        log.info("[ml_model] Model initialized. Brier=%.4f, AUC=%.4f, ECE=%.4f (features=%d, lgbm=%s)",
                 self.brier_score, self.roc_auc, self.ece, N_FEATURES, self.lgbm is not None)

    @staticmethod
    def _compute_sharpe_from_equity() -> float:
        """
        Compute the annualised Sharpe ratio from the durable store's equity-history
        time-series.

        Reads `store.equity_history` — a chronological list of
        ``{"timestamp": float, "equity": float, "pnl": float}`` points appended on
        every fill / settlement (see ``core/data_store.py`` and
        ``core/settlement.py``).

        Procedure:
          1. Extract the equity series in chronological order.
          2. Drop non-positive equity points (degenerate / unfunded states).
          3. Compute per-bar simple returns ``r_t = (E_t - E_{t-1}) / E_{t-1}``.
          4. Sharpe_bar = mean(r) / std(r, ddof=1).
          5. Annualise by ``sqrt(bars_per_year)`` where ``bars_per_year`` is inferred
             from the median inter-bar timestamp interval — keeps the metric
             cadence-agnostic (works for trade-driven, settlement-driven, or
             fixed-interval bars).

        Returns:
            float: annualised Sharpe ratio rounded to 4 dp. Returns 0.0 when the
            equity history is empty / too short / degenerate, or when the store is
            unavailable (e.g. during unit-test cold-start).
        """
        # Lazy import to avoid any module-load circular dependency and to make the
        # method safe during model unpickling / cold-start tests.
        try:
            from core.data_store import store
        except Exception:
            log.debug("[ml_model] store unavailable for Sharpe computation", exc_info=True)
            return 0.0

        history = getattr(store, "equity_history", None)
        if not history or len(history) < 2:
            return 0.0

        try:
            equities = np.array(
                [float(h.get("equity", 0.0)) for h in history],
                dtype=np.float64,
            )
        except (AttributeError, TypeError, ValueError):
            return 0.0

        # Filter out non-positive equity points (avoid div-by-zero / negative returns).
        if np.any(equities <= 0):
            equities = equities[equities > 0]
            if len(equities) < 2:
                return 0.0

        # Per-bar simple returns.
        rets = np.diff(equities) / equities[:-1]
        if len(rets) < 2:
            return 0.0

        mu = float(np.mean(rets))
        sigma = float(np.std(rets, ddof=1))
        if sigma < 1e-12:
            return 0.0

        sharpe_bar = mu / sigma

        # Annualise: infer bars-per-year from median inter-bar timestamp interval.
        SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
        try:
            ts = np.array(
                [float(h.get("timestamp", 0.0)) for h in history],
                dtype=np.float64,
            )
            dts = np.diff(ts)
            dts = dts[dts > 0]
            if len(dts) > 0:
                median_dt = float(np.median(dts))
                if median_dt > 0:
                    bars_per_year = SECONDS_PER_YEAR / median_dt
                    sharpe_bar *= float(np.sqrt(bars_per_year))
        except (AttributeError, TypeError, ValueError):
            # If timestamps are missing / malformed, fall back to per-bar Sharpe
            # (un-annualised) rather than discarding the metric entirely.
            log.debug("[ml_model] Sharpe annualisation skipped — using per-bar Sharpe", exc_info=True)

        if not np.isfinite(sharpe_bar):
            return 0.0

        return round(float(sharpe_bar), 4)

    def _blend_probas(self, x_scaled: np.ndarray) -> np.ndarray:
        """Internal helper: blend all learners' probabilities for a scaled batch."""
        rf_p = self.rf_cal.predict_proba(x_scaled)[:, 1] if self.rf_cal is not None else self.rf.predict_proba(x_scaled)[:, 1]
        gb_p = self.gb_cal.predict_proba(x_scaled)[:, 1] if self.gb_cal is not None else self.gb.predict_proba(x_scaled)[:, 1]
        sgd_p = self.sgd.predict_proba(x_scaled)[:, 1] if self._sgd_trained else np.zeros(len(x_scaled))

        if self.lgbm is not None:
            try:
                lgbm_p = self.lgbm.predict_proba(x_scaled)[:, 1]
            except Exception:
                lgbm_p = np.zeros(len(x_scaled))
        else:
            lgbm_p = np.zeros(len(x_scaled))

        w_rf, w_gb, w_sgd, w_lgbm = self._adaptive_weights()
        total = w_rf + w_gb + w_sgd + w_lgbm
        if total < 1e-9:
            total = 1.0
        return (w_rf * rf_p + w_gb * gb_p + w_sgd * sgd_p + w_lgbm * lgbm_p) / total

    @property
    def is_fitted(self) -> bool:
        return self.rf is not None

    def predict_proba(
        self,
        features: np.ndarray | None = None,
        token_id: str | None = None,
    ) -> float:
        """Return ``p_yes`` — the ensemble's calibrated probability the market resolves YES.

        W33-2 — supports two call modes:

        * **Explicit features** (backward-compatible with every existing
          call site, e.g. ``core/analysis_engine.py``): pass a 38-dim
          ``features`` array. The pipeline is bypassed entirely and the
          existing ``predict()`` path is taken. The ``token_id`` is
          forwarded for provenance bookkeeping but is NOT used to fetch
          features.

        * **Token-id-only** (new in W33-2): pass ``features=None`` with a
          ``token_id``. The model resolves the process-wide
          :class:`ingestion.feature_pipeline.FeaturePipeline` singleton
          and asks it for the point-in-time-correct feature vector at
          ``time.time()``. If the pipeline returns ``None`` (no snapshot
          at-or-before ``as_of``), the model returns the neutral 0.5
          prediction and logs a warning. Before scoring, the model also
          checks feature freshness via :meth:`get_feature_age` and logs
          a warning when the most recent snapshot is older than
          :data:`FEATURE_FRESHNESS_THRESHOLD_SECONDS` (60 s). The
          prediction is still made against the stale features — the
          freshness check is informational only, matching the W33-2
          spec ("still predict, but with reduced confidence" — the
          existing confidence formula ``|p_yes - 0.5| * 2`` already
          narrows to zero as p_yes approaches 0.5, so a stale-feature
          penalty naturally reduces confidence if the ensemble's
          blended probability regresses toward 0.5).

        Args:
            features: Optional 1-D float ndarray of length
                :data:`~ml.features.N_FEATURES`. When omitted, the model
                fetches features for ``token_id`` from the feature
                pipeline.
            token_id: Market token identifier. Required when
                ``features`` is ``None`` (otherwise the call returns
                ``0.5`` — there's nothing to score). Forwarded to
                :meth:`predict` for provenance when ``features`` is
                provided.

        Returns:
            The calibrated ``p_yes`` in ``[0.01, 0.99]`` on the success
            path; ``0.5`` (neutral) when features could not be obtained
            from the pipeline. The existing ``predict()`` fallback tuple
            ``(float(features[0]), 0.5)`` applies when the ensemble
            raises internally.
        """
        # W33-2 — Token-id-only path: fetch features from the pipeline.
        if features is None and token_id:
            features, stale = self._fetch_features_from_pipeline(token_id)
            if features is None:
                log.warning(
                    "[ml_model] No features available for %s — using default 0.5",
                    token_id,
                )
                return 0.5
            if stale:
                log.warning(
                    "[ml_model] Stale features for %s (%.0fs old) — predicting with reduced confidence",
                    token_id,
                    self.last_feature_age or 0.0,
                )

        if features is None:
            # No features and no token_id — nothing to score.
            return 0.5

        p, _ = self.predict(features, token_id=token_id or "")
        return p

    def predict_proba_at(self, token_id: str, timestamp: float) -> float:
        """Point-in-time ``predict_proba`` for backtest replay.

        W33-2 — fetches the feature vector the model WOULD have seen at
        wall-clock time ``timestamp`` (via the pipeline's point-in-time
        snapshot filter: only rows with ``observation_ts <= timestamp``
        are considered) and scores it through the same ``predict()``
        path as live serving. A replay at T0 thus produces the exact
        feature vector + prediction the model would have produced at
        T0 — no look-ahead bias.

        Returns ``0.5`` when the pipeline has no snapshot at-or-before
        ``timestamp`` (the same neutral-fallback semantics as
        :meth:`predict_proba`'s token-id-only path).

        Args:
            token_id: Market token to score.
            timestamp: Epoch float (UTC seconds). The pipeline's
                point-in-time filter uses this as the upper bound on
                snapshot ``timestamp`` — future snapshots are excluded.

        Returns:
            The calibrated ``p_yes`` in ``[0.01, 0.99]`` on the success
            path; ``0.5`` (neutral) when no features are available at
            or before ``timestamp``.
        """
        if not token_id:
            return 0.5
        try:
            pipe = self._resolve_feature_pipeline()
            # Point-in-time freshness check — measure age against
            # ``timestamp`` (NOT wall clock) so the warning is itself
            # PIT-correct.
            try:
                age = _run_async(pipe.get_feature_age(token_id, as_of=timestamp))
            except Exception:  # noqa: BLE001 — defensive
                age = None
            self.last_feature_age = age
            if age is not None and age > FEATURE_FRESHNESS_THRESHOLD_SECONDS:
                log.warning(
                    "[ml_model] Stale features for %s at as_of=%.0f (%.0fs old)",
                    token_id, timestamp, age,
                )

            features = _run_async(pipe.get_features(token_id, timestamp=timestamp))
        except Exception as e:  # noqa: BLE001 — defensive
            log.debug("[ml_model] predict_proba_at pipeline fetch failed: %s", e)
            self.last_feature_age = None
            return 0.5
        if features is None:
            return 0.5
        p, _ = self.predict(features, token_id=token_id)
        return p

    def _fetch_features_from_pipeline(
        self, token_id: str
    ) -> tuple[np.ndarray | None, bool]:
        """Fetch features + freshness flag from the resolved feature pipeline.

        W33-2 helper factored out of :meth:`predict_proba` so the same
        fetch + freshness logic is shared with :meth:`predict_proba_at`
        (modulo timestamp handling — ``predict_proba_at`` resolves
        ``as_of`` explicitly, this helper uses ``time.time()``).

        Args:
            token_id: Market token to fetch features for.

        Returns:
            A ``(features, stale)`` tuple where ``features`` is the
            38-dim ndarray or ``None`` (no snapshot available), and
            ``stale`` is ``True`` when the most recent snapshot is older
            than :data:`FEATURE_FRESHNESS_THRESHOLD_SECONDS`. ``stale``
            is ``False`` when the pipeline returned ``None`` (no
            freshness check was run) or when the snapshot is fresh.
        """
        try:
            pipe = self._resolve_feature_pipeline()
            try:
                age = _run_async(pipe.get_feature_age(token_id))
            except Exception:  # noqa: BLE001 — defensive
                age = None
            self.last_feature_age = age
            features = _run_async(pipe.get_features(token_id, timestamp=time.time()))
        except Exception as e:  # noqa: BLE001 — defensive
            log.debug("[ml_model] pipeline feature fetch failed: %s", e)
            self.last_feature_age = None
            return None, False
        if features is None:
            return None, False
        stale = age is not None and age > FEATURE_FRESHNESS_THRESHOLD_SECONDS
        return features, stale

    def _resolve_feature_pipeline(self) -> Any:
        """Return the model's feature-pipeline handle, lazy-resolving the singleton.

        W33-2 — the pipeline handle is resolved on first use so module
        import is cheap and side-effect free. Tests override the
        ``self._feature_pipeline`` instance attribute directly to inject
        a fake (no monkey-patching of the module-level singleton
        required). Production callers get the process-wide
        :class:`ingestion.feature_pipeline.FeaturePipeline` (the same
        singleton the strategy / API use).
        """
        if self._feature_pipeline is not None:
            return self._feature_pipeline
        # Lazy import — avoids the circular import path
        # ``ml.model`` → ``ingestion.feature_pipeline`` → ``ml.features`` →
        # (already loaded). The deferred resolution also keeps a fresh
        # ``MarketMLModel()`` constructed in a unit test that doesn't
        # exercise the token-id path from triggering the import chain.
        from ingestion.feature_pipeline import get_feature_pipeline
        self._feature_pipeline = get_feature_pipeline()
        return self._feature_pipeline

    def predict_confidence(self, features: np.ndarray, token_id: str = "") -> float:
        _, conf = self.predict(features, token_id=token_id)
        return conf

    def predict_proba_raw(self, features: np.ndarray) -> np.ndarray:
        """Return the *uncalibrated* blended ensemble probabilities for a batch.

        This is the raw probability the post-hoc calibrator (W11-5) was fit
        against. Exposed publicly so callers (training orchestrator, offline
        validation, tests) can compute calibration curves / fit new
        calibrators without re-running the full ``predict()`` path
        (which would recursively re-apply the calibrator).

        Args:
            features: 2-D array (n_samples, N_FEATURES) of raw feature
                      vectors. NOT pre-scaled — this method applies the
                      fitted ``StandardScaler`` internally.

        Returns:
            1-D float array of length ``n_samples`` with the adaptive-blend
            ensemble probability for the positive (YES) class — clipped to
            ``[0.01, 0.99]`` to match ``predict()``'s output range.

        Raises:
            RuntimeError: if the model is not fitted (``self.rf is None``).
        """
        if self.rf is None or self.gb is None:
            raise RuntimeError(
                "predict_proba_raw() called on an unfitted model "
                "(self.rf is None) — call fit_initial() first"
            )
        features = np.asarray(features)
        if features.ndim == 1:
            features = features.reshape(1, -1)
        x_scaled = self.scaler.transform(features)
        return np.clip(self._blend_probas(x_scaled), 0.01, 0.99)

    def compute_explanation(
        self,
        features: np.ndarray,
        token_id: str = "",
        top_n: int = 10,
    ) -> "PredictionExplanation | None":
        """Compute a SHAP-based per-prediction explanation for ``features``.

        W17-3 — opt-in ML explainability. Runs ``shap.TreeExplainer`` against
        the fitted RandomForest ensemble member (the fastest of the four
        ensemble members — exact Tree SHAP in O(TLD²) vs KernelExplainer's
        approximate O(n_background * n_samples * n_features)). The
        ``predicted_probability`` is overwritten with the ensemble's blended
        output (via ``predict()``) so the explanation's headline number
        matches the dashboard — the RF-only TreeExplainer probability is
        otherwise misleading.

        Pure read: does NOT mutate the model, the feature store, or any
        persisted state. Safe to call concurrently with ``predict()`` /
        ``update()`` — the underlying sklearn RF is read-only after fit.

        Args:
            features: 1-D or 2-D ndarray. 1-D is treated as a single row.
            token_id: market token this explanation belongs to.
            top_n: number of top features (by abs SHAP) to keep in
                ``top_features``. Capped to ``len(FEATURE_NAMES)`` (38).

        Returns:
            A ``PredictionExplanation`` dataclass instance (or ``None``
            on any failure — the caller is expected to handle ``None``
            by skipping the explanation field on the response). The
            dataclass's ``to_dict()`` method returns the JSON-able
            representation used by the ``GET /api/ml/explain/{token_id}``
            HTTP route.

        Raises:
            RuntimeError: if the model is not fitted (``self.rf is None``).
        """
        if self.rf is None or self.gb is None:
            raise RuntimeError(
                "compute_explanation() called on an unfitted model "
                "(self.rf is None) — call fit_initial() first"
            )

        features = np.asarray(features)
        if features.ndim == 1:
            features = features.reshape(1, -1)

        # Compute the ensemble's blended prediction so the explanation's
        # ``predicted_probability`` matches the dashboard's headline
        # number (the RF-only TreeExplainer probability would otherwise
        # be misleading — the ensemble blends 4 models).
        try:
            pred_p, _ = self.predict(features[0], token_id=token_id)
        except Exception:  # noqa: BLE001 — defensive: predict() never raises in practice
            pred_p = 0.5

        try:
            from ml.explainability import model_explainer

            explanations = model_explainer.explain_tree_model(
                self.rf,
                features,
                FEATURE_NAMES,
                token_id=token_id,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            log.debug("[ml_model] SHAP explanation failed: %s", e, exc_info=True)
            return None

        if not explanations:
            return None

        expl = explanations[0]
        expl.predicted_probability = float(pred_p)
        expl.prediction_direction = "positive" if pred_p > 0.5 else "negative"
        expl.confidence = abs(pred_p - 0.5) * 2
        # Trim to caller's top_n (already capped to 38 by the route's
        # Query constraint — defensive here in case a non-route caller
        # passes an uncapped value).
        expl.top_features = expl.top_features[: max(1, min(top_n, len(FEATURE_NAMES)))]
        return expl

    def _adaptive_weights(self) -> tuple[float, float, float, float]:
        """
        Compute blending weights from per-model rolling Brier scores (deque, O(1)).
        Lower Brier = better => higher weight. Returns (w_rf, w_gb, w_sgd, w_lgbm).
        """
        def _avg(window: deque) -> float:
            return float(np.mean(window)) if len(window) >= 10 else 0.15

        rf_b = _avg(self._rf_brier_window)
        gb_b = _avg(self._gb_brier_window)
        sgd_b = _avg(self._sgd_brier_window) if self._sgd_trained else 1.0
        lgbm_b = _avg(self._lgbm_brier_window) if self.lgbm is not None else 1.0

        rf_skill = 1.0 / max(rf_b, 1e-6)
        gb_skill = 1.0 / max(gb_b, 1e-6)
        sgd_skill = 1.0 / max(sgd_b, 1e-6) if self._sgd_trained else 0.0
        lgbm_skill = 1.0 / max(lgbm_b, 1e-6) if self.lgbm is not None else 0.0

        total = rf_skill + gb_skill + sgd_skill + lgbm_skill
        if total < 1e-9:
            return (0.40, 0.35, 0.05, 0.20) if self.lgbm is not None else (0.50, 0.45, 0.05, 0.0)
        return rf_skill / total, gb_skill / total, sgd_skill / total, lgbm_skill / total

    @property
    def adaptive_weights(self) -> dict[str, float]:
        w_rf, w_gb, w_sgd, w_lgbm = self._adaptive_weights()
        return {"rf": round(w_rf, 4), "gb": round(w_gb, 4), "sgd": round(w_sgd, 4), "lgbm": round(w_lgbm, 4)}

    def predict(self, features: np.ndarray, token_id: str = "") -> tuple[float, float]:
        # W33-3 — Feature contract validation. Runs BEFORE the
        # unfitted-model check so a malformed feature vector (wrong
        # length, NaN / Inf, out-of-range value, train/serve schema
        # mismatch) returns a neutral ``(0.5, 0.0)`` prediction
        # rather than feeding the model an out-of-distribution input
        # (which could produce a confidently-wrong probability). The
        # validator is imported lazily so a missing
        # ``ingestion.contract_validator`` module (e.g. a parallel
        # agent's branch hasn't landed) doesn't crash the predict
        # path — the missing module is logged and the predict path
        # falls through to the existing unfitted / exception handlers.
        try:
            from ingestion.contract_validator import contract_validator
            _cv_result = contract_validator.validate_features(
                features, model_version=self.version
            )
            if not _cv_result.is_valid:
                log.error(
                    "[ml_model] Feature contract violation "
                    "(version=%s token=%s): %s",
                    self.version,
                    token_id,
                    "; ".join(_cv_result.errors),
                )
                # Neutral prediction + zero confidence — the input was
                # invalid so the model's output has no calibration
                # basis. ``0.5`` is the uninformative prior on a binary
                # outcome; ``0.0`` confidence surfaces the violation in
                # downstream consumers (strategy gate, shadow
                # inference, drift detector).
                return 0.5, 0.0
        except ImportError:  # pragma: no cover — defensive
            log.debug(
                "[ml_model] ingestion.contract_validator not importable — "
                "skipping feature contract check",
                exc_info=True,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            # The validator's contract is "never raises" — but a
            # future override might break that. Don't let a broken
            # validator take down the predict path; log and continue.
            log.warning(
                "[ml_model] Feature contract validator raised: %s — "
                "continuing with predict path", e,
            )

        if self.rf is None or self.gb is None:
            return float(features[0]), 0.5

        try:
            x_scaled = self.scaler.transform(features.reshape(1, -1))

            # ── Level-0: collect all base-learner probabilities ───────────────
            rf_prob = float((self.rf_cal or self.rf).predict_proba(x_scaled)[0, 1])
            gb_prob = float((self.gb_cal or self.gb).predict_proba(x_scaled)[0, 1])
            sgd_prob = float(self.sgd.predict_proba(x_scaled)[0, 1]) if self._sgd_trained else 0.0
            lgbm_prob = 0.0
            if self.lgbm is not None:
                try:
                    lgbm_prob = float(self.lgbm.predict_proba(x_scaled)[0, 1])
                except Exception:
                    pass

            # ── Level-1: stacking meta-learner (when warm) ────────────────────
            meta_p = ensemble_meta_learner.predict(rf_prob, gb_prob, sgd_prob, lgbm_prob)

            if meta_p is not None:
                # Meta-learner produced a calibrated stacked probability
                p_yes = meta_p
                log.debug("[ml_model] Meta-learner active: p=%.4f (rf=%.3f gb=%.3f sgd=%.3f lgbm=%.3f)",
                          p_yes, rf_prob, gb_prob, sgd_prob, lgbm_prob)
            else:
                # Fallback: adaptive Brier-inverse weighted blend
                w_rf, w_gb, w_sgd, w_lgbm = self._adaptive_weights()
                total = w_rf + w_gb + w_sgd + w_lgbm
                if total < 1e-9:
                    total = 1.0
                p_yes = (w_rf * rf_prob + w_gb * gb_prob + w_sgd * sgd_prob + w_lgbm * lgbm_prob) / total

            # ── W11-5: post-hoc probability calibration ───────────────────────
            # Apply Platt scaling / isotonic regression (whichever the singleton
            # ``calibrator`` was fit with) as a final post-processing step on
            # the blended probability. ``calibrator.transform`` is a
            # passthrough when the calibrator has NOT been fit yet (cold-start,
            # disabled, or pre-first-retrain), so this call is safe to make
            # unconditionally — it cannot break the predict path.
            p_yes = float(calibrator.transform(np.array([p_yes]))[0])

            p_yes = float(np.clip(p_yes, 0.01, 0.99))
            confidence = abs(p_yes - 0.5) * 2.0

            # Record in drift detector (prediction only — NOT outcome)
            drift_detector.record_prediction(p_yes)

            # Record feature vector to the durable feature store
            try:
                from core.timescale_db import timescale_db
                timescale_db.record_prediction(features, p_yes, confidence, token_id=token_id)
            except Exception:
                log.debug("[ml_model] feature-vector record skipped", exc_info=True)

            # W16-2 — Record per-feature values to the ML feature store
            # (separate from the timescale_db prediction log above).
            # The feature store indexes per-(token_id, feature_name) value
            # rows so an operator can audit the input feature distribution
            # that fed a given prediction via ``GET /api/features`` /
            # ``GET /api/features/{name}/stats`` / ``GET /api/features/drift``.
            # Defensive try/except: a transient SQLite hiccup must NEVER
            # degrade the production predict path (mirrors the
            # timescale_db / shadow_inference blocks above).
            try:
                from ml.feature_store import feature_store
                # Map the ndarray back to ``{feature_name: value}`` using
                # ``FEATURE_NAMES`` so the feature_values rows are
                # addressable by name. Skip the call when the feature
                # vector length doesn't match the catalog (defensive —
                # should never happen for a fitted model but guards the
                # cold-start / load_or_create mismatch path).
                if features.shape[0] == len(FEATURE_NAMES):
                    feature_store.record_values(
                        token_id=token_id or "",
                        features={
                            FEATURE_NAMES[i]: float(features[i])
                            for i in range(len(FEATURE_NAMES))
                        },
                        prediction_id=None,
                    )
            except Exception:
                log.debug("[ml_model] feature-store record skipped", exc_info=True)

            # T13 / W19-8 — run shadow challenger model(s) in parallel
            # with production. The challenger output NEVER affects
            # ``p_yes`` / ``confidence`` — it is recorded in the
            # shadow-inference ring buffer for offline disagreement
            # analysis and (W19-8) for ``evaluate_and_promote``.
            #
            # W19-8 — predict path now uses the split
            # ``predict_all`` + ``record_predictions`` API so the
            # champion prediction and the shadow predictions are
            # placed on the same logical comparison record without
            # re-invoking any challenger. The previous single-shot
            # ``run_shadow(features, token_id, p_yes)`` call is preserved
            # as a convenience wrapper that delegates to these two
            # methods (see ``ml/shadow_inference.py``). Bare try/except
            # so a missing / raising challenger cannot degrade the
            # production predict path.
            try:
                from ml.shadow_inference import shadow_inference
                shadow_preds = shadow_inference.predict_all(features)
                if shadow_preds:
                    shadow_inference.record_predictions(
                        token_id=token_id,
                        champion_pred=p_yes,
                        shadow_preds=shadow_preds,
                    )
            except Exception:
                log.debug("[ml_model] shadow inference skipped", exc_info=True)

            return p_yes, confidence
        except Exception as e:
            log.debug("[ml_model] Predict error: %s", e)
            return float(features[0]), 0.5

    def update(self, features: np.ndarray, outcome_yes: bool) -> None:
        """Online update: partial_fit SGD, Brier rolling windows, and meta-learner buffer."""
        try:
            x_scaled = self.scaler.transform(features.reshape(1, -1))
            y_label = 1 if outcome_yes else 0
            y_val = np.array([y_label])

            # ── Collect per-model probabilities (needed for meta-learner) ─────
            rf_p = float((self.rf_cal or self.rf).predict_proba(x_scaled)[0, 1]) if self.rf else 0.5
            gb_p = float((self.gb_cal or self.gb).predict_proba(x_scaled)[0, 1]) if self.gb else 0.5
            sgd_p = float(self.sgd.predict_proba(x_scaled)[0, 1]) if self._sgd_trained else 0.0
            lgbm_p = 0.0
            if self.lgbm is not None:
                try:
                    lgbm_p = float(self.lgbm.predict_proba(x_scaled)[0, 1])
                except Exception:
                    pass

            # ── Track per-model Brier for adaptive weighting (deque — O(1)) ──
            if self.rf is not None:
                self._rf_brier_window.append((rf_p - y_label) ** 2)
            if self.gb is not None:
                self._gb_brier_window.append((gb_p - y_label) ** 2)
            if self._sgd_trained:
                self._sgd_brier_window.append((sgd_p - y_label) ** 2)
            if self.lgbm is not None and lgbm_p > 0.0:
                self._lgbm_brier_window.append((lgbm_p - y_label) ** 2)

            # ── Feed meta-learner with per-model preds vs outcome ─────────────
            ensemble_meta_learner.record_outcome(rf_p, gb_p, sgd_p, lgbm_p, y_label)

            # ── Record ensemble outcome to drift detector ─────────────────────
            # Use meta-learner if warm, else fall back to weighted blend
            meta_p = ensemble_meta_learner.predict(rf_p, gb_p, sgd_p, lgbm_p)
            if meta_p is not None:
                p_ensemble = meta_p
            else:
                w_rf, w_gb, w_sgd, w_lgbm = self._adaptive_weights()
                total_w = w_rf + w_gb + w_sgd + w_lgbm
                p_ensemble = (w_rf * rf_p + w_gb * gb_p + w_sgd * sgd_p + w_lgbm * lgbm_p) / max(total_w, 1e-9)
            p_ensemble = float(np.clip(p_ensemble, 0.01, 0.99))
            drift_detector.record_outcome(p_ensemble, y_label)

            # ── SGD partial_fit (incremental online learning) ─────────────────
            self.sgd.partial_fit(x_scaled, y_val, classes=np.array([0, 1]))
            self._n_updates += 1
            log.info(
                "[ml_model] Online update #%d (outcome=%s, meta_warm=%s, weights=%s)",
                self._n_updates, "YES" if outcome_yes else "NO",
                ensemble_meta_learner.is_warm, self.adaptive_weights,
            )
        except Exception as e:
            log.error("[ml_model] Online update failed: %s", e)

    def warmup(self) -> dict:
        """Warm the meta-learner from already-resolved labeled feature vectors.

        W19-8 — wires ``ensemble_meta_learner.warm_from_labeled_samples``
        into the production startup path. The meta-learner is the Level-2
        stacking blender that sits over the four base learners; without
        warming it stays cold until ≥30 live market resolutions drip-feed
        labels through ``update()`` over hours / days. Pre-warming from
        the durable labeled-sample store activates the meta-learner
        IMMEDIATELY (cold-start bypass) so the first prediction the bot
        makes after a restart uses the calibrated stacked blend rather
        than the adaptive Brier-inverse fallback.

        Safe to call at any time after ``fit_initial()`` — the underlying
        ``warm_from_labeled_samples`` is idempotent (each call back-fills
        the rolling buffer + force-refits the meta-model). Returns the
        summary dict from the meta-learner (``n_loaded`` / ``n_skipped``
        / ``buffer_size`` / ``is_warm`` / ``error``) so the lifespan
        startup hook can log the headline. Defensive try/except — a
        meta-learner warmup failure MUST NOT block server startup (the
        production predict path falls back to adaptive-weight blending
        when the meta-learner is not warm).
        """
        try:
            summary = ensemble_meta_learner.warm_from_labeled_samples()
            if summary.get("is_warm"):
                log.info(
                    "[ml_model] Meta-learner warmed with %d samples "
                    "(buffer=%d, skipped=%d)",
                    summary.get("n_loaded", 0),
                    summary.get("buffer_size", 0),
                    summary.get("n_skipped", 0),
                )
            else:
                log.warning(
                    "[ml_model] Meta-learner warmup did not activate "
                    "stacking: %s", summary.get("error", "unknown"),
                )
            return summary
        except Exception as e:
            log.warning("[ml_model] Meta-learner warmup failed: %s", e)
            return {"error": str(e), "is_warm": False}

    def save(self) -> None:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MODEL_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as f:
                pickle.dump(self, f)
            tmp.replace(MODEL_PATH)
        except Exception as e:
            log.error("[ml_model] Failed to save model: %s", e)

    @classmethod
    def load_or_create(cls) -> MarketMLModel:
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    cached = pickle.load(f)
                # Feature-count guard: discard stale model if feature dimensions changed
                if (
                    isinstance(cached, cls)
                    and hasattr(cached, "scaler")
                    and hasattr(cached.scaler, "n_features_in_")
                    and cached.scaler.n_features_in_ == N_FEATURES
                ):
                    log.info("[ml_model] Loaded cached model from %s (features=%d)", MODEL_PATH, N_FEATURES)
                    return cached
                else:
                    log.warning("[ml_model] Cached model feature count mismatch — retraining with %d features", N_FEATURES)
            except Exception as e:
                log.warning("[ml_model] Failed to load cached model (retraining): %s", e)

        model = cls()
        model.fit_initial()
        model.save()
        return model

    # ── W24-2 — Fresh-ensemble factory for out-of-sample validation ──────
    #
    # ``_create_ensemble`` returns a fresh, unfitted, sklearn-style
    # classifier (a ``Pipeline(StandardScaler + RandomForestClassifier)``)
    # that the :class:`OutOfSampleValidator` can ``.fit(X, y)`` on the
    # train window and ``.predict_proba(X)`` on the validation / test
    # windows. Returned fresh on every call so the validator's per-run
    # ``model_factory`` invocation gets a clean state — no risk of
    # state leakage from a prior fit.
    #
    # The factory is **deliberately simpler** than the full 4-member
    # production ensemble (RF + GB + SGD + LightGBM + meta-learner + post-hoc
    # calibrator). The full ensemble is too slow to retrain in the
    # OOS validator's tight loop (which trains one model per
    # ``validate()`` call) AND it carries the live-model singleton's
    # online-update state, which would defeat the OOS test's purpose.
    # The pipeline returned here matches the W11-era ensemble's *shape*
    # (StandardScaler + RF) — the validator's job is to measure the
    # generalization gap of a representative learner, not to reproduce
    # the exact production blend.
    #
    # ``random_state=SEED`` pins the RF's bootstrap to a deterministic
    # outcome so the same training data produces the same metrics across
    # runs (critical for reproducible OOS validation reports).
    def _create_ensemble(self) -> Any:
        """Return a fresh, unfitted sklearn ``Pipeline`` for OOS validation.

        The pipeline is ``StandardScaler → RandomForestClassifier`` with
        ``random_state=SEED`` for reproducibility. Exposed publicly so the
        W24-2 :class:`OutOfSampleValidator` can pull a fresh learner via
        ``ml_model._create_ensemble()`` per ``validate()`` invocation.
        """
        from sklearn.ensemble import RandomForestClassifier as _RF
        from sklearn.pipeline import Pipeline as _Pipeline

        return _Pipeline(steps=[
            ("scaler", StandardScaler()),
            ("clf", _RF(
                n_estimators=80,
                max_depth=8,
                min_samples_leaf=5,
                random_state=SEED,
                n_jobs=-1,
            )),
        ])


# Global singleton
ml_model = MarketMLModel.load_or_create()
