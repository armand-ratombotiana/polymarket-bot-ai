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
function and the endpoints below are fully self-contained and exercise
only the existing ``model_registry`` singleton surface (``list_versions``,
``rollback``, ``active_version``, ``versions``).

W23-7 — the registry's lifecycle surface (``promote`` / ``demote`` /
``get_lifecycle``) is exposed via four additional endpoints
(``GET /api/ml/lifecycle``, ``POST /api/ml/{version}/promote``,
``POST /api/ml/{version}/rollback``, ``POST /api/ml/{version}/demote``)
registered alongside the T8 endpoints below. The new endpoints exercise
only the additive lifecycle methods on the existing ``model_registry``
singleton — no production prediction path is touched.

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
                "state": "champion",
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

  GET /api/ml/lifecycle
      Return the registered model-version lineage with lifecycle states
      (``experimental`` / ``shadow`` / ``challenger`` / ``champion`` /
      ``demoted`` / ``retired``), newest-first. Lightweight view of
      :meth:`ModelRegistry.get_lifecycle` — carries only the subset of
      fields an operator needs to assess the model fleet's lifecycle
      progression without pulling the full metric payload (the full payload
      is available via ``GET /api/ml/versions``).

  POST /api/ml/{version}/promote
      Promote a model one step along the lifecycle ladder
      (``experimental → shadow → challenger → champion``). When promoted to
      ``champion``, the current champion (if any) is demoted and
      ``active_version`` is re-pointed at the target. Returns 200 on
      success; 400 if the version is unknown OR is already at a terminal
      state (``champion`` / ``demoted`` / ``retired``).

  POST /api/ml/{version}/rollback
      Lifecycle-aware rollback: demote the current champion and crown the
      target as the new champion. Differs from ``POST /api/ml/rollback`` (T8)
      in that it uses a path parameter and reports the lifecycle state
      transition; both endpoints invoke the same underlying
      ``model_registry.rollback(version)`` method, so they are functionally
      equivalent — this is the lifecycle-style alias that mirrors the
      ``promote`` / ``demote`` URL shape. Returns 200 on success; 400 if
      the version is unknown.

  POST /api/ml/{version}/demote
      Demote a model one step along the demotion ladder
      (``champion → demoted → retired``). Returns 200 on success; 400 if
      the version is unknown OR is at a state the demotion ladder does not
      traverse (``experimental`` / ``shadow`` / ``challenger`` /
      ``retired``).
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

    # ── W23-7 — Model lifecycle management endpoints ─────────────────────────
    # Four additive endpoints that surface the registry's lifecycle-state
    # machinery (``promote`` / ``demote`` / ``rollback`` /
    # ``get_lifecycle``) over HTTP. Each endpoint exercises only the
    # additive lifecycle methods on the existing ``model_registry``
    # singleton — no production prediction path is touched. The T8
    # endpoints above (``GET /api/ml/versions`` + ``POST
    # /api/ml/rollback?version=...``) are preserved unchanged so the
    # existing dashboard / CLI tooling that consumes the query-param
    # rollback shape keeps working.

    @app.get("/api/ml/lifecycle", tags=["ml"])
    async def _get_model_lifecycle():
        """Return all model versions with their lifecycle states, newest-first.

        Each entry carries ``version``, ``state`` (one of
        ``experimental`` / ``shadow`` / ``challenger`` / ``champion`` /
        ``demoted`` / ``retired``), ``status`` (safety-gate verdict —
        ``ACTIVE`` / ``REJECTED``), ``brier``, ``auc``, ``n_samples``,
        ``created_at``, and ``is_active`` — the subset of fields an
        operator needs to assess the model fleet's lifecycle progression
        without pulling the full metric payload (the full payload is
        available via ``GET /api/ml/versions``).
        """
        return model_registry.get_lifecycle()

    @app.post("/api/ml/{version}/promote", tags=["ml"])
    async def _promote_model(version: str):
        """Promote a model one step along the lifecycle ladder.

        ``experimental → shadow → challenger → champion``. When the model
        reaches ``champion``, the current champion (if any) is demoted to
        ``demoted`` and ``active_version`` is re-pointed at the target.

        Returns 400 if the version is unknown OR is already at a terminal
        state (``champion`` / ``demoted`` / ``retired``) — the
        ``promote`` call would be a destructive no-op in those cases.
        """
        if model_registry.promote(version):
            new_state = model_registry.get_state(version)
            return {
                "ok": True,
                "version": version,
                "state": new_state,
                "active_version": model_registry.get_active_version(),
            }
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot promote model '{version}' — version not found OR "
                f"already at a terminal state (champion / demoted / retired)."
            ),
        )

    @app.post("/api/ml/{version}/rollback", tags=["ml"])
    async def _rollback_model_lifecycle(version: str):
        """Lifecycle-aware rollback: demote current champion, crown target.

        Mirrors the T8 ``POST /api/ml/rollback?version=...`` endpoint but
        uses a path parameter and reports the lifecycle-state transition.
        Both invoke the same ``model_registry.rollback(version)`` method;
        this is the lifecycle-style alias that mirrors the
        ``promote`` / ``demote`` URL shape.

        Returns 400 if the version is not in the registry lineage.
        """
        previous = model_registry.get_active_version()
        if model_registry.rollback(version):
            # Best-effort audit record — mirrors the T8 endpoint's
            # audit-logging contract so operator-initiated rollbacks via
            # EITHER URL are recorded in the durable audit trail.
            try:
                from core.audit_logger import audit_logger
                await audit_logger.log_event(
                    category="ml",
                    event_type="model_rollback_lifecycle",
                    details=(
                        f"lifecycle rollback: active_version {previous} -> "
                        f"{version} (state -> champion)"
                    ),
                )
            except Exception as e:  # noqa: BLE001 — audit is best-effort
                log.warning(
                    "[ml.routes] audit log failed for lifecycle rollback "
                    "%s -> %s: %s",
                    previous, version, e,
                )
            return {
                "ok": True,
                "version": version,
                "state": "champion",
                "previous_version": previous,
                "active_version": version,
            }
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot rollback to model '{version}' — version not found "
                f"in model registry lineage."
            ),
        )

    @app.post("/api/ml/{version}/demote", tags=["ml"])
    async def _demote_model(version: str):
        """Demote a model one step along the demotion ladder.

        ``champion → demoted → retired``. Returns 400 if the version is
        unknown OR is at a state the demotion ladder does not traverse
        (``experimental`` / ``shadow`` / ``challenger`` / ``retired``).
        """
        if model_registry.demote(version):
            new_state = model_registry.get_state(version)
            return {
                "ok": True,
                "version": version,
                "state": new_state,
                "active_version": model_registry.get_active_version(),
            }
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot demote model '{version}' — version not found OR "
                f"not in a demotable state (champion / demoted)."
            ),
        )


__all__ = ["register_routes"]
