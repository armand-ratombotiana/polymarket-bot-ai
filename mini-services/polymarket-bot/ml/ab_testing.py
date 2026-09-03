"""A/B testing framework for ML models.

Splits prediction traffic between champion (current production model)
and challenger (new model being tested). Tracks outcomes to determine
if the challenger is statistically significantly better.

Usage:
    from ml.ab_testing import ab_test
    ab_test.start_experiment(
        name="v2_isotonic_vs_v1",
        champion_version="v1",
        challenger_version="v2_isotonic",
        traffic_split=0.3,  # 30% to challenger
    )

Design contract (W14-5):

  * **Additive / opt-in.** The A/B framework is fully self-contained in
    this module — no production prediction path is modified. Callers
    (e.g. ``strategies/signal_trader.py::_ml_signal``) opt in by asking
    ``ab_test.assign_model(token_id)`` which version to invoke; if no
    experiment is running the call returns the string ``"champion"`` so
    the caller can fall back to its existing production model.

  * **Deterministic assignment.** ``assign_model(token_id)`` hashes the
    ``token_id`` so the same token always gets the same model within an
    experiment. This avoids the contamination that would arise if a
    token could be predicted by both the champion and challenger on
    different invocations.

  * **Persistent SQLite store.** Experiment metadata + per-prediction
    rows live in a SQLite db (``AB_TEST_DB_PATH`` env var, default
    ``/app/data/ab_tests.db``). Outcomes can be back-filled later via
    ``update_outcome(token_id, actual_outcome)`` once the market
    resolves.

  * **Statistical evaluation.** ``evaluate(experiment_name)`` runs a
    two-proportion z-test on accuracy (champion vs challenger) and a
    two-sample t-test on per-row Brier scores. Reports p-values and a
    promote/keep_champion recommendation. Requires ``min_samples``
    observations per arm before it returns a verdict.

  * **HTTP surface.** ``register_routes(app)`` appends four endpoints
    under ``/api/ab-test`` for operator control + inspection. Auth
    enforced by the caller's existing ``enforce_api_auth`` middleware
    (none of the paths are in ``PUBLIC_PATHS``).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

# Pydantic models for the HTTP surface are declared at module level (not
# inside ``register_routes``) so FastAPI can resolve their forward references
# when generating the OpenAPI schema. The FastAPI / pydantic imports are
# kept lazy via a ``try/except`` so this module can still be imported in
# non-server contexts (e.g. unit tests of ``ABTestManager``).
try:  # pragma: no cover — exercised only when the web framework is installed
    from fastapi import HTTPException, Query
    from pydantic import BaseModel, Field

    class StartExperimentRequest(BaseModel):
        name: str = Field(..., description="Human-readable experiment name")
        champion_version: str = Field(..., description="Production model version id")
        challenger_version: str = Field(..., description="Candidate model version id")
        traffic_split: float = Field(
            0.3, description="Fraction of traffic routed to the challenger [0, 1]"
        )
        min_samples: int = Field(
            100, description="Min samples per arm before evaluate() returns a verdict"
        )

    class StopExperimentRequest(BaseModel):
        name: Optional[str] = Field(
            None, description="Experiment name (defaults to currently-active)"
        )
        reason: str = Field("manual", description="Free-text reason for the stop")

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover — fastapi absent in non-server envs
    StartExperimentRequest = None  # type: ignore[assignment]
    StopExperimentRequest = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    _FASTAPI_AVAILABLE = False

logger = logging.getLogger(__name__)

AB_TEST_DB_PATH = Path(os.environ.get("AB_TEST_DB_PATH", "/app/data/ab_tests.db"))


@dataclass
class Experiment:
    """In-memory descriptor for a running (or recently-stopped) experiment."""

    name: str
    champion_version: str
    challenger_version: str
    traffic_split: float  # Fraction going to challenger (0-1)
    status: str = "running"  # running, completed, stopped
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    min_samples: int = 100  # Min samples before evaluation
    confidence_level: float = 0.95


class ABTestManager:
    """Manages A/B testing experiments."""

    def __init__(self, db_path: Path = AB_TEST_DB_PATH):
        self._db_path = db_path
        self._init_db()
        self._current_experiment = self._load_active_experiment()

    # ── Schema / state bootstrap ────────────────────────────────────────────
    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    name TEXT PRIMARY KEY,
                    champion_version TEXT NOT NULL,
                    challenger_version TEXT NOT NULL,
                    traffic_split REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    min_samples INTEGER DEFAULT 100,
                    confidence_level REAL DEFAULT 0.95,
                    config TEXT
                );

                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    token_id TEXT,
                    prediction REAL NOT NULL,
                    actual_outcome INTEGER,
                    timestamp REAL NOT NULL,
                    metadata TEXT,
                    FOREIGN KEY (experiment_name) REFERENCES experiments(name)
                );

                CREATE INDEX IF NOT EXISTS idx_pred_exp ON predictions(experiment_name);
                CREATE INDEX IF NOT EXISTS idx_pred_version ON predictions(model_version);
                """
            )

    def _load_active_experiment(self) -> Optional[Experiment]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM experiments WHERE status = 'running' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return Experiment(
                    name=row["name"],
                    champion_version=row["champion_version"],
                    challenger_version=row["challenger_version"],
                    traffic_split=row["traffic_split"],
                    status=row["status"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    min_samples=row["min_samples"],
                    confidence_level=row["confidence_level"],
                )
        return None

    # ── Experiment lifecycle ──────────────────────────────────────────────────
    def start_experiment(
        self,
        name: str,
        champion_version: str,
        challenger_version: str,
        traffic_split: float = 0.3,
        min_samples: int = 100,
    ) -> Experiment:
        """Start a new A/B test experiment."""
        # Stop any running experiment
        if self._current_experiment:
            self.stop_experiment(self._current_experiment.name)

        exp = Experiment(
            name=name,
            champion_version=champion_version,
            challenger_version=challenger_version,
            traffic_split=traffic_split,
            min_samples=min_samples,
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO experiments
                (name, champion_version, challenger_version, traffic_split,
                 status, started_at, min_samples, confidence_level)
                VALUES (?, ?, ?, ?, 'running', ?, ?, 0.95)
                """,
                (
                    name,
                    champion_version,
                    challenger_version,
                    traffic_split,
                    exp.started_at,
                    min_samples,
                ),
            )

        self._current_experiment = exp
        logger.info(
            "Started A/B test: %s (champion=%s, challenger=%s, split=%s)",
            name, champion_version, challenger_version, traffic_split,
        )
        return exp

    def stop_experiment(self, name: str, reason: str = "manual") -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE experiments SET status = 'stopped', ended_at = ? "
                "WHERE name = ? AND status = 'running'",
                (time.time(), name),
            )
            if cursor.rowcount > 0:
                logger.info("Stopped A/B test: %s (%s)", name, reason)
                if self._current_experiment and self._current_experiment.name == name:
                    self._current_experiment = None
                return True
        return False

    # ── Traffic assignment + recording ───────────────────────────────────────
    def assign_model(self, token_id: str = None) -> str:
        """Decide which model version to use for this prediction.

        Uses traffic_split to route to challenger, rest to champion.
        Deterministic per-token (same token always gets same model) to avoid
        contamination.
        """
        if not self._current_experiment:
            return "champion"  # No experiment running

        exp = self._current_experiment

        # Deterministic assignment based on token_id hash (if provided).
        # Python's built-in hash() is randomly seeded per-process under
        # PYTHONHASHSEED=1; we use a stable sha256-derived fold instead so
        # the same token maps to the same arm across processes / restarts
        # (a process-seeded hash would re-shuffle every restart, defeating
        # the deterministic-assignment contract).
        if token_id:
            import hashlib

            digest = hashlib.sha256(str(token_id).encode("utf-8")).hexdigest()
            hash_val = int(digest[:8], 16) % 1000 / 1000.0
            use_challenger = hash_val < exp.traffic_split
        else:
            use_challenger = np.random.random() < exp.traffic_split

        return exp.challenger_version if use_challenger else exp.champion_version

    def record_prediction(
        self,
        model_version: str,
        prediction: float,
        token_id: str = None,
        actual_outcome: int = None,
        metadata: dict = None,
    ) -> None:
        """Record a prediction (and optionally its outcome)."""
        if not self._current_experiment:
            return

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO predictions
                (experiment_name, model_version, token_id, prediction,
                 actual_outcome, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._current_experiment.name,
                    model_version,
                    token_id,
                    prediction,
                    actual_outcome,
                    time.time(),
                    json.dumps(metadata or {}),
                ),
            )

    def update_outcome(self, token_id: str, actual_outcome: int) -> None:
        """Update the actual outcome for predictions on a token."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE predictions SET actual_outcome = ? "
                "WHERE token_id = ? AND actual_outcome IS NULL",
                (actual_outcome, token_id),
            )

    # ── Evaluation ───────────────────────────────────────────────────────────
    def evaluate(self, experiment_name: str = None) -> dict:
        """Evaluate an experiment — compute statistical significance."""
        name = experiment_name or (
            self._current_experiment.name if self._current_experiment else None
        )
        if not name:
            return {"error": "No experiment to evaluate"}

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Get experiment
            exp = conn.execute(
                "SELECT * FROM experiments WHERE name = ?", (name,)
            ).fetchone()
            if not exp:
                return {"error": "Experiment not found"}

            # Get predictions with outcomes
            champ_preds = conn.execute(
                "SELECT prediction, actual_outcome FROM predictions "
                "WHERE experiment_name = ? AND model_version = ? "
                "AND actual_outcome IS NOT NULL",
                (name, exp["champion_version"]),
            ).fetchall()

            chall_preds = conn.execute(
                "SELECT prediction, actual_outcome FROM predictions "
                "WHERE experiment_name = ? AND model_version = ? "
                "AND actual_outcome IS NOT NULL",
                (name, exp["challenger_version"]),
            ).fetchall()

            if (
                len(champ_preds) < exp["min_samples"]
                or len(chall_preds) < exp["min_samples"]
            ):
                return {
                    "experiment": name,
                    "status": "insufficient_data",
                    "champion_samples": len(champ_preds),
                    "challenger_samples": len(chall_preds),
                    "min_required": exp["min_samples"],
                }

            # Compute metrics
            champ_metrics = self._compute_metrics(
                np.array([r["prediction"] for r in champ_preds]),
                np.array([r["actual_outcome"] for r in champ_preds]),
            )
            chall_metrics = self._compute_metrics(
                np.array([r["prediction"] for r in chall_preds]),
                np.array([r["actual_outcome"] for r in chall_preds]),
            )

            # Statistical significance test (two-proportion z-test on accuracy)
            from scipy import stats

            champ_acc = champ_metrics["accuracy"]
            chall_acc = chall_metrics["accuracy"]
            n1, n2 = len(champ_preds), len(chall_preds)
            p_pool = (champ_acc * n1 + chall_acc * n2) / (n1 + n2)
            se = np.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
            z_score = (chall_acc - champ_acc) / (se + 1e-8)
            p_value = 2 * (1 - stats.norm.cdf(abs(z_score)))

            # Brier score comparison (t-test)
            champ_briers = (
                np.array([r["prediction"] for r in champ_preds])
                - np.array([r["actual_outcome"] for r in champ_preds])
            ) ** 2
            chall_briers = (
                np.array([r["prediction"] for r in chall_preds])
                - np.array([r["actual_outcome"] for r in chall_preds])
            ) ** 2
            t_stat, brier_p_value = stats.ttest_ind(chall_briers, champ_briers)

            return {
                "experiment": name,
                "status": "evaluated",
                "champion": {
                    "version": exp["champion_version"],
                    "samples": len(champ_preds),
                    **champ_metrics,
                },
                "challenger": {
                    "version": exp["challenger_version"],
                    "samples": len(chall_preds),
                    **chall_metrics,
                },
                "significance": {
                    "accuracy_z_score": float(z_score),
                    "accuracy_p_value": float(p_value),
                    "brier_t_statistic": float(t_stat),
                    "brier_p_value": float(brier_p_value),
                    # Cast numpy.bool_ -> python bool so json.dumps +
                    # FastAPI's JSONResponse serialise as ``true``/``false``
                    # rather than falling back to ``"True"`` (string).
                    "is_significant": bool(
                        p_value < (1 - exp["confidence_level"])
                    ),
                    "challenger_is_better": bool(
                        chall_acc > champ_acc and p_value < 0.05
                    ),
                },
                "recommendation": (
                    "promote"
                    if chall_acc > champ_acc and p_value < 0.05
                    else (
                        "keep_champion"
                        if p_value >= 0.05
                        else (
                            "promote"
                            if chall_metrics["brier"] < champ_metrics["brier"]
                            else "keep_champion"
                        )
                    )
                ),
            }

    def _compute_metrics(self, preds: np.ndarray, labels: np.ndarray) -> dict:
        from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

        try:
            auc = roc_auc_score(labels, preds)
        except Exception:
            auc = 0.5
        try:
            brier = brier_score_loss(labels, preds)
        except Exception:
            brier = 0.25
        try:
            ll = log_loss(labels, np.clip(preds, 1e-6, 1 - 1e-6))
        except Exception:
            ll = 0.693
        accuracy = float(np.mean((preds > 0.5).astype(int) == labels))
        return {
            "auc": float(auc),
            "brier": float(brier),
            "log_loss": float(ll),
            "accuracy": accuracy,
        }

    # ── Status / inspection ──────────────────────────────────────────────────
    def get_status(self) -> dict:
        if not self._current_experiment:
            return {"active": False, "experiments": self._list_experiments()}

        exp = self._current_experiment
        with sqlite3.connect(self._db_path) as conn:
            champ_count = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE experiment_name = ? "
                "AND model_version = ?",
                (exp.name, exp.champion_version),
            ).fetchone()[0]
            chall_count = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE experiment_name = ? "
                "AND model_version = ?",
                (exp.name, exp.challenger_version),
            ).fetchone()[0]

        return {
            "active": True,
            "experiment": {
                "name": exp.name,
                "champion_version": exp.champion_version,
                "challenger_version": exp.challenger_version,
                "traffic_split": exp.traffic_split,
                "started_at": exp.started_at,
                "min_samples": exp.min_samples,
            },
            "champion_predictions": champ_count,
            "challenger_predictions": chall_count,
            "experiments": self._list_experiments(),
        }

    def _list_experiments(self) -> list:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM experiments ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
            return [dict(r) for r in rows]


# Singleton
ab_test = ABTestManager()


# ── HTTP surface ────────────────────────────────────────────────────────────
def register_routes(app: Any) -> None:
    """Append A/B test management endpoints to a FastAPI app.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing ``enforce_api_auth``
    middleware (these paths are not in ``PUBLIC_PATHS``).

    Endpoints:

      GET  /api/ab-test
          Return the current A/B test status — whether an experiment is
          active, the champion / challenger versions, the traffic split,
          per-arm prediction counts, and the most recent ~20 experiments.

      POST /api/ab-test/start
          Start a new experiment. Request body::

              {
                "name": "v2_isotonic_vs_v1",
                "champion_version": "v1",
                "challenger_version": "v2_isotonic",
                "traffic_split": 0.3,
                "min_samples": 100
              }

          ``traffic_split`` must be in [0, 1] (else 400). Starting a new
          experiment while one is already running stops the previous one
          (only one experiment is active at a time).

      POST /api/ab-test/stop
          Stop the current (or named) experiment. Request body::

              {"name": "v2_isotonic_vs_v1", "reason": "manual"}
              // or
              {"reason": "promoted_to_champion"}

          If ``name`` is omitted, the currently-active experiment is
          targeted. Returns 404 if no matching running experiment exists.

      GET  /api/ab-test/evaluate?experiment_name=<name>
          Evaluate an experiment — compute per-arm metrics (auc / brier /
          log_loss / accuracy), two-proportion z-test on accuracy,
          two-sample t-test on per-row Brier scores, and a
          promote/keep_champion recommendation. Returns 404 if the
          experiment is not found.
    """
    # Local imports are now redundant — FastAPI / pydantic were promoted to
    # module-level imports (with a try/except fallback) so the request
    # models can be resolved by FastAPI when it builds the OpenAPI schema.
    # ``HTTPException`` and ``Query`` are imported at module scope alongside
    # ``StartExperimentRequest`` / ``StopExperimentRequest``.
    if not _FASTAPI_AVAILABLE:  # pragma: no cover — defensive
        return

    @app.get("/api/ab-test", tags=["ml"])
    async def _ab_test_status():
        """Return the current A/B test status (active experiment + recent history)."""
        return ab_test.get_status()

    @app.post("/api/ab-test/start", tags=["ml"])
    async def _ab_test_start(req: StartExperimentRequest):
        """Start a new A/B test experiment (stops any running experiment)."""
        if not (0.0 <= req.traffic_split <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="traffic_split must be in [0, 1]",
            )
        exp = ab_test.start_experiment(
            name=req.name,
            champion_version=req.champion_version,
            challenger_version=req.challenger_version,
            traffic_split=req.traffic_split,
            min_samples=req.min_samples,
        )
        return {
            "started": True,
            "experiment": {
                "name": exp.name,
                "champion_version": exp.champion_version,
                "challenger_version": exp.challenger_version,
                "traffic_split": exp.traffic_split,
                "started_at": exp.started_at,
                "min_samples": exp.min_samples,
            },
        }

    @app.post("/api/ab-test/stop", tags=["ml"])
    async def _ab_test_stop(req: StopExperimentRequest):
        """Stop the current (or named) A/B test experiment."""
        name = req.name or (
            ab_test._current_experiment.name if ab_test._current_experiment else None
        )
        if not name:
            raise HTTPException(
                status_code=404,
                detail="No active A/B test experiment to stop",
            )
        stopped = ab_test.stop_experiment(name, reason=req.reason)
        if not stopped:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Experiment '{name}' not found or already stopped"
                ),
            )
        return {"stopped": True, "name": name, "reason": req.reason}

    @app.get("/api/ab-test/evaluate", tags=["ml"])
    async def _ab_test_evaluate(
        experiment_name: Optional[str] = Query(
            None,
            description=(
                "Experiment name to evaluate. Defaults to the currently-"
                "active experiment (or returns 404 if none is active)."
            ),
        ),
    ):
        """Evaluate an A/B test experiment (per-arm metrics + significance)."""
        result = ab_test.evaluate(experiment_name)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result


__all__ = ["Experiment", "ABTestManager", "ab_test", "register_routes", "AB_TEST_DB_PATH"]
