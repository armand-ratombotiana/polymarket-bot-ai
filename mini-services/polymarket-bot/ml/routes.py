"""
ml/routes.py — ML model-governance HTTP surface (version lineage + rollback).

Additive route registration mirroring the ``register_routes(app)`` pattern
established by ``core/observability.py``, ``core/execution_quality.py``,
``core/closed_positions.py``, and ``core/attribution.py`` — each of which is
wired into the live FastAPI server via a single trailing import + call at the
bottom of ``api/server.py``. Per the T8 task contract, ``api/server.py`` is
**not** edited by this change; the caller is expected to add one line::

    from ml.routes import register_routes as _register_ml_governance_routes
    _register_ml_governance_routes(app)

when wiring these endpoints into the live server. The ``register_routes``
function and the two endpoints below are fully self-contained and exercise
only the existing ``model_registry`` singleton surface (``list_versions``,
``rollback``, ``active_version``, ``versions``).

Endpoints (auth-protected by the caller's existing ``enforce_api_auth``
bearer-token middleware — these paths are not in ``PUBLIC_PATHS``):

  GET /api/ml/versions
      Return the full registered model-version lineage with metrics.
      Response shape::

          {
            "active_version": "v1.champion",
            "total_registered": 5,
            "versions": [
              {
                "version": "v1.champion",
                "created_at": 1788409517.69,
                "brier_score": 0.1013,
                "roc_auc": 0.9451,
                "ece": 0.0836,
                "sharpe_ratio": 0.0,
                "status": "ACTIVE",
                "n_samples": 3000,
                "parameters": { ... },
                "is_active": true
              },
              ...
            ]
          }

      Each entry is the :meth:`ModelVersionRecord.to_dict` payload enriched
      with an ``is_active`` flag identifying the currently promoted version.

  POST /api/ml/rollback?version=v1.xxx.0
      Roll the active model version back to a previously registered version.
      On success returns 200 with the previous + new active version and the
      target version's full metric payload. Returns 404 if the requested
      version is not in the registry lineage. The rollback is also recorded
      in the durable audit trail (``core/audit_logger``) for governance /
      post-incident review — best-effort, never blocks the response.
"""
from __future__ import annotations

import logging
from typing import Any

from ml.model_registry import model_registry

log = logging.getLogger(__name__)


def register_routes(app: Any) -> None:
    """
    Append ML model-governance endpoints to a FastAPI app.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing
    ``enforce_api_auth`` middleware (these paths are not in ``PUBLIC_PATHS``).
    """
    # Local import — FastAPI is optional at module load so that ``ml.routes``
    # can be imported in non-server contexts (e.g. unit tests of
    # ``model_registry`` itself) without pulling in the web framework.
    from fastapi import HTTPException, Query

    @app.get("/api/ml/versions", tags=["ml"])
    async def _list_ml_versions():
        """Return all registered model versions with full metrics (newest first)."""
        versions = model_registry.list_versions()
        return {
            "active_version": model_registry.active_version,
            "total_registered": len(versions),
            "versions": versions,
        }

    @app.post("/api/ml/rollback", tags=["ml"])
    async def _rollback_ml_model(
        version: str = Query(
            ...,
            description=(
                "Target version to roll back to (e.g. 'v1.155.0'). Must "
                "already exist in the model registry lineage — rollback "
                "cannot resurrect an un-registered or pruned version."
            ),
        ),
    ):
        """Roll the active model version back to a previously registered version."""
        previous = model_registry.active_version
        ok = model_registry.rollback(version)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Version '{version}' not found in model registry lineage; "
                    f"rollback refused. Current active_version='{previous}'."
                ),
            )

        # Best-effort durable audit record. Wrapped in try/except so a
        # transient audit-DB hiccup never fails an otherwise-successful
        # rollback — the registry's own WARNING/INFO logs are the source
        # of truth; this audit row is a governance convenience.
        try:
            from core.audit_logger import audit_logger
            await audit_logger.log_event(
                category="ml",
                event_type="model_rollback",
                details=f"active_version rolled back {previous} -> {version}",
            )
        except Exception as e:  # noqa: BLE001 — audit is best-effort
            log.warning(
                "[ml.routes] audit log failed for rollback %s -> %s: %s",
                previous, version, e,
            )

        target = next(
            (v for v in model_registry.versions if v.version == version),
            None,
        )
        return {
            "rolled_back": True,
            "previous_version": previous,
            "active_version": version,
            "target_metrics": target.to_dict() if target is not None else None,
        }


__all__ = ["register_routes"]
