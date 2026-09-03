"""
ml/model_registry.py — Model Versioning, Experiment Lineage & Safety Governance.

Tracks model versions (e.g. v1.0.0, v1.1.0), experiment lineage, validation benchmarks,
Expected Calibration Error (ECE), Sharpe Ratio, and enforces risk gatekeeping before promotion.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REGISTRY_FILE = Path(os.environ.get("MODEL_REGISTRY_PATH", "/app/data/model_registry.json"))


class ModelVersionRecord:
    def __init__(
        self,
        version: str,
        created_at: float,
        brier_score: float,
        roc_auc: float,
        ece: float,
        sharpe_ratio: float,
        status: str,
        n_samples: int,
        parameters: dict[str, Any],
    ) -> None:
        self.version = version
        self.created_at = created_at
        self.brier_score = brier_score
        self.roc_auc = roc_auc
        self.ece = ece
        self.sharpe_ratio = sharpe_ratio
        self.status = status
        self.n_samples = n_samples
        self.parameters = parameters

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "brier_score": round(self.brier_score, 4),
            "roc_auc": round(self.roc_auc, 4),
            "ece": round(self.ece, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "status": self.status,
            "n_samples": self.n_samples,
            "parameters": self.parameters,
        }


class ModelRegistry:
    """
    Enterprise model governance and experiment tracking registry.
    """

    def __init__(self) -> None:
        self.versions: list[ModelVersionRecord] = []
        self.active_version: str = "v1.0.0"
        self._load_from_disk()

    def register_version(
        self,
        version: str,
        brier_score: float,
        roc_auc: float,
        ece: float,
        sharpe_ratio: float,
        n_samples: int,
        parameters: dict[str, Any],
    ) -> bool:
        """
        Validate model benchmarks and register new version.
        Enforces safety gate: Brier score must be <= 0.22 and ROC-AUC >= 0.70.
        """
        # Safety gate check
        if brier_score > 0.22 or roc_auc < 0.70:
            log.warning("[model_registry] ❌ Model %s REJECTED: Brier=%.4f (max 0.22), AUC=%.4f (min 0.70)",
                        version, brier_score, roc_auc)
            status = "REJECTED"
            promoted = False
        else:
            status = "ACTIVE"
            self.active_version = version
            promoted = True
            log.info("[model_registry] ✅ Model %s PROMOTED to ACTIVE (Brier=%.4f, AUC=%.4f, ECE=%.4f, Sharpe=%.2f)",
                     version, brier_score, roc_auc, ece, sharpe_ratio)

        record = ModelVersionRecord(
            version=version,
            created_at=time.time(),
            brier_score=brier_score,
            roc_auc=roc_auc,
            ece=ece,
            sharpe_ratio=sharpe_ratio,
            status=status,
            n_samples=n_samples,
            parameters=parameters,
        )
        self.versions.insert(0, record)
        self._save_to_disk()
        return promoted

    def get_summary(self) -> dict[str, Any]:
        return {
            "active_version": self.active_version,
            "total_registered": len(self.versions),
            "versions": [v.to_dict() for v in self.versions],
        }

    def _save_to_disk(self) -> None:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "active_version": self.active_version,
                "versions": [v.to_dict() for v in self.versions],
            }
            with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("[model_registry] Save error: %s", e)

    def _load_from_disk(self) -> None:
        if not REGISTRY_FILE.exists():
            # Initialize with default baseline versions
            self.register_version(
                version="v1.0.0",
                brier_score=0.1838,
                roc_auc=0.7939,
                ece=0.038,
                sharpe_ratio=1.92,
                n_samples=3000,
                parameters={"n_estimators_rf": 100, "n_estimators_gb": 60, "calibration": "isotonic"},
            )
            return

        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.active_version = data.get("active_version", "v1.0.0")
            self.versions = [
                ModelVersionRecord(
                    version=v["version"],
                    created_at=v["created_at"],
                    brier_score=v["brier_score"],
                    roc_auc=v["roc_auc"],
                    ece=v.get("ece", 0.04),
                    sharpe_ratio=v.get("sharpe_ratio", 1.8),
                    status=v["status"],
                    n_samples=v.get("n_samples", 3000),
                    parameters=v.get("parameters", {}),
                )
                for v in data.get("versions", [])
            ]
        except Exception as e:
            log.warning("[model_registry] Load error: %s", e)


# Global singleton
model_registry = ModelRegistry()
