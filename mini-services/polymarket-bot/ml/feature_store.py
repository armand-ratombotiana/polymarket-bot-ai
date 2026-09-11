"""ML feature store — tracks feature definitions, values, and importance.

Records:
- Feature definitions (name, type, description, computation)
- Feature values per prediction (for audit)
- Feature importance per model version (for drift detection)
- Feature statistics (mean, std, min, max, percentiles)

Design contract (W16-2):

  * **Additive / opt-in.** The feature store is fully self-contained in
    this module — no production prediction path is modified except for
    two narrow call sites in ``ml/model.py`` (``predict()`` records the
    feature values; ``fit_initial()`` records the per-version importance
    snapshot). Both call sites are wrapped in defensive ``try/except``
    so a transient SQLite hiccup NEVER degrades the predict path.

  * **Persistent SQLite store.** Definitions + per-prediction values +
    per-version importance snapshots + computed statistics live in a
    SQLite db (``FEATURE_STORE_DB`` env var, default
    ``/app/data/feature_store.db``). The ``feature_values`` table is
    indexed on ``token_id``, ``feature_name``, and ``timestamp DESC``
    so the most-recent-N lookup for a feature is O(log n + k).

  * **Per-prediction audit trail.** ``record_values(token_id, features,
    prediction_id)`` writes one row per numeric feature value. This is
    the row-level input for ``compute_stats`` and
    ``detect_feature_drift`` (windowed mean-shift test) — independent
    of the higher-level PSI / KS drift detector in
    ``ml/drift_detector.py`` (which monitors the model's prediction
    distribution, not the input feature distribution).

  * **Per-version importance snapshot.** ``record_importance(model_version,
    importance_dict)`` sorts the dict by descending importance, assigns
    a rank, and persists one row per feature. The history table is
    indexed on ``model_version`` so the dashboard can pull the lineage
    for a specific version in one seek.

  * **HTTP surface.** ``register_routes(app)`` appends five endpoints
    under ``/api/features`` for operator inspection + internal
    ingestion. Auth is enforced by the caller's existing
    ``enforce_api_auth`` middleware (none of the paths are in
    ``PUBLIC_PATHS``).
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

logger = logging.getLogger(__name__)

FEATURE_STORE_DB = Path(os.environ.get("FEATURE_STORE_DB", "/app/data/feature_store.db"))


@dataclass
class FeatureDefinition:
    name: str
    type: str  # "numeric", "categorical", "boolean"
    description: str
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class FeatureImportance:
    feature_name: str
    model_version: str
    importance: float
    rank: int
    timestamp: float


class FeatureStore:
    """SQLite-backed feature store."""

    def __init__(self, db_path: Path = FEATURE_STORE_DB):
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS feature_definitions (
                    name TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    description TEXT,
                    min_value REAL,
                    max_value REAL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feature_values (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id TEXT,
                    feature_name TEXT NOT NULL,
                    value REAL,
                    timestamp REAL NOT NULL,
                    prediction_id TEXT,
                    FOREIGN KEY (feature_name) REFERENCES feature_definitions(name)
                );

                CREATE INDEX IF NOT EXISTS idx_fv_token ON feature_values(token_id);
                CREATE INDEX IF NOT EXISTS idx_fv_feature ON feature_values(feature_name);
                CREATE INDEX IF NOT EXISTS idx_fv_ts ON feature_values(timestamp DESC);

                CREATE TABLE IF NOT EXISTS feature_importance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    importance REAL NOT NULL,
                    rank INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    UNIQUE(feature_name, model_version, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_fi_version ON feature_importance(model_version);

                CREATE TABLE IF NOT EXISTS feature_stats (
                    feature_name TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    mean REAL,
                    std REAL,
                    min REAL,
                    max REAL,
                    p25 REAL,
                    p50 REAL,
                    p75 REAL,
                    p95 REAL,
                    n_samples INTEGER,
                    PRIMARY KEY (feature_name, timestamp)
                );
            """)

    def register_feature(
        self,
        name: str,
        type: str,  # noqa: A002 — shadowing ``type`` is harmless in this register context
        description: str = "",
        min_value: float = None,
        max_value: float = None,
    ) -> None:
        """Register (or upsert) a feature definition."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO feature_definitions
                (name, type, description, min_value, max_value, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, type, description, min_value, max_value, time.time()),
            )

    def record_values(self, token_id: str, features: dict, prediction_id: str = None) -> None:
        """Record feature values for a prediction."""
        ts = time.time()
        with sqlite3.connect(self._db_path) as conn:
            for name, value in features.items():
                if isinstance(value, (int, float)):
                    conn.execute(
                        """
                        INSERT INTO feature_values (token_id, feature_name, value, timestamp, prediction_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (token_id, name, float(value), ts, prediction_id),
                    )

    def record_importance(self, model_version: str, importance_dict: dict[str, float]) -> None:
        """Record feature importance for a model version."""
        ts = time.time()
        sorted_features = sorted(importance_dict.items(), key=lambda x: -x[1])
        with sqlite3.connect(self._db_path) as conn:
            for rank, (name, imp) in enumerate(sorted_features, 1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO feature_importance
                    (feature_name, model_version, importance, rank, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, model_version, float(imp), rank, ts),
                )

    def compute_stats(self, feature_name: str, since_hours: float = 24) -> dict:
        """Compute statistics for a feature over the last N hours."""
        cutoff = time.time() - since_hours * 3600
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT value FROM feature_values WHERE feature_name = ? AND timestamp > ?",
                (feature_name, cutoff),
            ).fetchall()

        if not rows:
            return {"feature": feature_name, "n_samples": 0}

        values = np.array([r[0] for r in rows])
        return {
            "feature": feature_name,
            "n_samples": len(values),
            "mean": float(np.mean(values)),
            "std": float(np.std(values)),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "p25": float(np.percentile(values, 25)),
            "p50": float(np.percentile(values, 50)),
            "p75": float(np.percentile(values, 75)),
            "p95": float(np.percentile(values, 95)),
        }

    def get_importance_history(
        self,
        feature_name: str = None,
        model_version: str = None,
        limit: int = 20,
    ) -> list:
        """Get feature importance history."""
        query = "SELECT * FROM feature_importance"
        params: list = []
        conditions: list[str] = []
        if feature_name:
            conditions.append("feature_name = ?")
            params.append(feature_name)
        if model_version:
            conditions.append("model_version = ?")
            params.append(model_version)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def get_top_features(self, model_version: str, top_n: int = 20) -> list:
        """Get top N most important features for a model version."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT feature_name, importance, rank, timestamp
                FROM feature_importance
                WHERE model_version = ? AND rank <= ?
                ORDER BY rank ASC
                """,
                (model_version, top_n),
            ).fetchall()
            return [dict(r) for r in rows]

    def detect_feature_drift(
        self,
        feature_name: str,
        reference_window_h: float = 168,
        current_window_h: float = 24,
    ) -> dict:
        """Detect drift in a feature by comparing distributions."""
        ref_stats = self.compute_stats(feature_name, reference_window_h)
        cur_stats = self.compute_stats(feature_name, current_window_h)

        if ref_stats.get("n_samples", 0) < 10 or cur_stats.get("n_samples", 0) < 10:
            return {"feature": feature_name, "status": "insufficient_data"}

        # Simple drift metric: normalized mean shift
        ref_std = ref_stats.get("std", 1) or 1
        mean_shift = abs(cur_stats["mean"] - ref_stats["mean"]) / ref_std

        return {
            "feature": feature_name,
            "reference_mean": ref_stats["mean"],
            "current_mean": cur_stats["mean"],
            "mean_shift_sigma": float(mean_shift),
            "status": "drifted" if mean_shift > 0.5 else "stable",
            "reference_samples": ref_stats["n_samples"],
            "current_samples": cur_stats["n_samples"],
        }

    def get_all_features(self) -> list:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM feature_definitions ORDER BY name").fetchall()]


# Singleton
feature_store = FeatureStore()


# ── HTTP surface ──────────────────────────────────────────────────────────────
# FastAPI / pydantic imports are kept lazy via a ``try/except`` so this module
# can still be imported in non-server contexts (e.g. unit tests of
# ``FeatureStore`` itself), mirroring the ``ml/ab_testing.py`` pattern.
try:  # pragma: no cover — exercised only when the web framework is installed
    from fastapi import HTTPException, Query
    from pydantic import BaseModel, Field

    class FeatureImportanceRequest(BaseModel):
        """Request body for ``POST /api/features/importance``.

        Lets an operator (or the training orchestrator's internal caller)
        push a per-version feature-importance snapshot. The body is the
        ``model_version`` plus a ``{feature_name: importance}`` dict; the
        endpoint sorts the dict by descending importance, assigns a rank
        per feature, and persists one row per feature.
        """

        model_version: str = Field(..., description="Model version id (e.g. 'v1.155.0')")
        importance: dict[str, float] = Field(
            ...,
            description="Mapping of feature_name -> importance score",
        )

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover — fastapi absent in non-server envs
    FeatureImportanceRequest = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    BaseModel = None  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]
    _FASTAPI_AVAILABLE = False


def register_routes(app: Any) -> None:
    """Append feature-store inspection + ingestion endpoints to a FastAPI app.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing ``enforce_api_auth``
    middleware (these paths are not in ``PUBLIC_PATHS``).

    Endpoints:

      GET  /api/features
          Return every registered feature definition (name, type,
          description, min/max bounds, created_at), sorted by name.

      GET  /api/features/{name}/stats
          Return windowed statistics (mean / std / min / max / p25 /
          p50 / p75 / p95 / n_samples) for a single feature over the
          last ``since_hours`` (query param, default 24h).

      GET  /api/features/importance
          Return feature-importance history. Optional query params:
          ``model_version`` (filter to one version), ``feature_name``
          (filter to one feature), ``limit`` (default 20, max 500).

      GET  /api/features/drift
          Return drift status for every registered feature by
          comparing the last 24h distribution against the last 168h
          (1-week) reference window. ``status`` is ``"drifted"`` when
          the mean shift exceeds 0.5σ, ``"stable"`` otherwise, or
          ``"insufficient_data"`` when either window has fewer than
          10 samples.

      POST /api/features/importance
          Record a feature-importance snapshot for a model version.
          Body shape::

              {
                "model_version": "v1.155.0",
                "importance": {"mid_price": 0.18, "spread_norm": 0.12, ...}
              }

          Used internally by the training orchestrator / ``fit_initial``
          in ``ml/model.py`` to capture the per-version importance
          lineage. Returns ``{"recorded": N}`` where ``N`` is the
          number of features persisted.
    """
    if not _FASTAPI_AVAILABLE:  # pragma: no cover — defensive
        return

    @app.get("/api/features", tags=["ml"])
    async def _list_features():
        """Return every registered feature definition, sorted by name."""
        return {"features": feature_store.get_all_features()}

    @app.get("/api/features/importance", tags=["ml"])
    async def _feature_importance_history(
        model_version: Optional[str] = Query(
            None,
            description="Filter to a single model version (e.g. 'v1.155.0').",
        ),
        feature_name: Optional[str] = Query(
            None,
            description="Filter to a single feature (e.g. 'mid_price').",
        ),
        limit: int = Query(
            20,
            ge=1,
            le=500,
            description="Maximum rows to return (newest first).",
        ),
    ):
        """Return feature-importance history, optionally filtered by version / feature."""
        return {
            "history": feature_store.get_importance_history(
                feature_name=feature_name,
                model_version=model_version,
                limit=limit,
            ),
        }

    @app.get("/api/features/drift", tags=["ml"])
    async def _feature_drift():
        """Return drift status for every registered feature.

        Each entry compares the last 24h against the last 168h (1-week)
        reference window. ``status`` is one of ``"drifted"`` /
        ``"stable"`` / ``"insufficient_data"``.
        """
        features = feature_store.get_all_features()
        drifts = [
            feature_store.detect_feature_drift(f["name"])
            for f in features
        ]
        return {
            "n_features": len(features),
            "drifts": drifts,
        }

    @app.get("/api/features/{name}/stats", tags=["ml"])
    async def _feature_stats(
        name: str,
        since_hours: float = Query(
            24.0,
            ge=0.0,
            le=24 * 365,
            description="Window length in hours (default 24h).",
        ),
    ):
        """Return windowed statistics for a single feature.

        Returns 404 if the feature name is not registered (so an
        operator typo doesn't silently return ``n_samples=0``).
        """
        # Look up the definition first so an unknown feature name yields
        # a 404 instead of an empty stats blob — keeps the contract
        # consistent with the rest of the API surface.
        with sqlite3.connect(feature_store._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM feature_definitions WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Feature '{name}' not registered",
            )
        stats = feature_store.compute_stats(name, since_hours=since_hours)
        return {**dict(row), **stats}

    @app.post("/api/features/importance", tags=["ml"])
    async def _record_importance(req: FeatureImportanceRequest):
        """Record a feature-importance snapshot for a model version."""
        feature_store.record_importance(req.model_version, req.importance)
        return {"recorded": len(req.importance)}


__all__ = [
    "FEATURE_STORE_DB",
    "FeatureDefinition",
    "FeatureImportance",
    "FeatureStore",
    "feature_store",
    "register_routes",
]
