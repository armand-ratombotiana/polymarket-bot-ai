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
from typing import Any, Optional

log = logging.getLogger(__name__)

REGISTRY_FILE = Path(os.environ.get("MODEL_REGISTRY_PATH", "/app/data/model_registry.json"))

# ── W23-7 — Model lifecycle states ─────────────────────────────────────────
# Models progress through a controlled lifecycle:
#
#   experimental  →  shadow  →  challenger  →  champion
#                                                ↓
#                                             demoted  →  retired
#
# * ``experimental`` — freshly registered, not yet validated against live
#   traffic or shadow traffic.
# * ``shadow``       — running alongside production via
#   ``ml/shadow_inference.py``; receives features + production predictions
#   but its outputs never affect trading decisions.
# * ``challenger``   — running in an A/B test via ``ml/ab_testing.py``;
#   receives a deterministic traffic split, outcomes are compared.
# * ``champion``     — the active production model. There is at most ONE
#   champion at a time (``active_version`` points at it).
# * ``demoted``      — was champion, has been superseded by a new champion
#   or rolled back from. Kept in the lineage for fast rollback.
# * ``retired``      — permanently out of service; kept in the lineage
#   for audit / reproducibility only.
#
# The ``status`` field (ACTIVE / REJECTED) captures the safety-gate verdict
# (Brier ≤ 0.22, AUC ≥ 0.70) and is ORTHOGONAL to the lifecycle ``state`` —
# a model can be ``status=REJECTED, state=experimental`` (failed the safety
# gate, never promoted) or ``status=ACTIVE, state=demoted`` (passed the
# safety gate, was champion, has since been superseded).
MODEL_STATE_EXPERIMENTAL = "experimental"
MODEL_STATE_SHADOW = "shadow"
MODEL_STATE_CHALLENGER = "challenger"
MODEL_STATE_CHAMPION = "champion"
MODEL_STATE_DEMOTED = "demoted"
MODEL_STATE_RETIRED = "retired"

# Ordered promotion ladder — each ``promote(version)`` call advances the
# model one step along this list. ``demote(version)`` walks the demotion
# ladder (champion → demoted → retired).
MODEL_PROMOTION_LADDER: tuple[str, ...] = (
    MODEL_STATE_EXPERIMENTAL,
    MODEL_STATE_SHADOW,
    MODEL_STATE_CHALLENGER,
    MODEL_STATE_CHAMPION,
)
MODEL_DEMOTION_LADDER: tuple[str, ...] = (
    MODEL_STATE_CHAMPION,
    MODEL_STATE_DEMOTED,
    MODEL_STATE_RETIRED,
)


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
        state: str = MODEL_STATE_EXPERIMENTAL,
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
        # W23-7 — lifecycle state (experimental / shadow / challenger /
        # champion / demoted / retired). Orthogonal to ``status`` (the
        # safety-gate verdict). Defaults to ``experimental`` for newly
        # registered versions; ``register_version`` upgrades this to
        # ``champion`` when the version passes the safety gate (and
        # demotes any previous champion).
        self.state = state

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
            "state": self.state,
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
            # W23-7 — failed the safety gate: lifecycle state stays at
            # ``experimental`` (operator can still drive it through the
            # shadow / challenger ladder manually via ``promote()`` if
            # they want to investigate despite the safety-gate failure —
            # ``rollback`` already allows this for the active version).
            state = MODEL_STATE_EXPERIMENTAL
        else:
            status = "ACTIVE"
            # W23-7 — demote the current champion (if any) before
            # promoting the new version, so there is at most ONE
            # champion in the lineage at a time.
            previous_champion = next(
                (v for v in self.versions if v.state == MODEL_STATE_CHAMPION),
                None,
            )
            if previous_champion is not None and previous_champion.version != version:
                previous_champion.state = MODEL_STATE_DEMOTED
                log.info(
                    "[model_registry] ⬇️ Previous champion %s demoted → demoted",
                    previous_champion.version,
                )
            self.active_version = version
            promoted = True
            state = MODEL_STATE_CHAMPION
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
            state=state,
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

    # ── T8: Version lineage inspection + operator-initiated rollback ──────────
    # Additive surface — no existing method altered. ``list_versions`` returns
    # the full lineage with metrics; ``rollback`` re-points ``active_version``
    # to a previously registered version. Both are consumed by the new
    # ``ml/routes.py`` HTTP surface (``GET /api/ml/versions`` and
    # ``POST /api/ml/rollback``).

    def list_versions(self) -> list[dict[str, Any]]:
        """
        Return every registered model version with its full metric payload,
        newest-first (the order in which ``register_version`` inserts).

        Each entry is the :meth:`ModelVersionRecord.to_dict` output enriched
        with an ``is_active`` flag so callers can immediately identify the
        currently promoted version without a separate ``active_version``
        lookup. This is the read-side counterpart to ``rollback`` and is
        surfaced verbatim by ``GET /api/ml/versions``.

        Returns:
            list[dict]: One dict per registered version. Never ``None``;
            empty only when the registry has never been seeded (which, by
            construction in ``_load_from_disk``, cannot happen — the
            baseline ``v1.0.0`` is always present).
        """
        return [
            {**v.to_dict(), "is_active": v.version == self.active_version}
            for v in self.versions
        ]

    def rollback(self, version: str) -> bool:
        """
        Roll the active model version back to a previously registered version.

        Looks up ``version`` in the registered lineage. If found, sets
        ``active_version`` to it, persists the change to disk via
        :meth:`_save_to_disk`, and returns ``True``. If the version is not
        registered, returns ``False`` and leaves all state untouched.

        W23-7 — lifecycle state transitions: rolling back to ``version``
        also demotes the current champion (``state: champion → demoted``)
        and sets the rolled-back target as the new champion
        (``state: * → champion``). The previous ``status`` field (ACTIVE /
        REJECTED — the safety-gate verdict) is preserved unchanged so the
        audit trail still reflects which models passed the safety gate at
        registration time. The two fields are orthogonal: ``status``
        records the safety-gate verdict, ``state`` records the lifecycle
        progression.

        Semantics & safety notes:

        - The target **must** already be in the registry lineage — rollback
          cannot resurrect an un-registered or pruned version. This is the
          "if it exists" guard from the T8 contract.
        - A rollback to a ``REJECTED`` model is permitted (operator-explicit
          override) but emits a ``WARNING`` log so the safety-gate bypass
          is observable in the audit trail. ``register_version`` blocks
          *automatic* promotion of rejected models; ``rollback`` is the
          human-in-the-loop escape hatch.
        - This method only re-points the registry's ``active_version``
          pointer and persists the JSON registry. It does **NOT** swap the
          in-memory ensemble weights / calibrated estimators — that is the
          responsibility of the model loader / training orchestrator, which
          reads ``active_version`` on its next reload cycle. The two-step
          contract (re-point → reload) is intentional: it keeps the
          registry a pure metadata store with no dependency on the heavy
          ML objects, mirroring the existing ``register_version`` design.

        Args:
            version: Target version string (e.g. ``"v1.155.0"``). Must
                match a previously registered ``ModelVersionRecord.version``.

        Returns:
            bool: ``True`` if the active version was (or already is) set to
            ``version``; ``False`` if ``version`` is not in the lineage.
        """
        target = next((v for v in self.versions if v.version == version), None)
        if target is None:
            log.warning(
                "[model_registry] ❌ Rollback FAILED: version %s not found "
                "in registry lineage (%d versions registered)",
                version, len(self.versions),
            )
            return False

        previous = self.active_version
        if previous == version:
            log.info(
                "[model_registry] ℹ️ Rollback no-op: version %s is already active",
                version,
            )
            return True

        if target.status == "REJECTED":
            log.warning(
                "[model_registry] ⚠️ Rollback to REJECTED model %s "
                "(Brier=%.4f, AUC=%.4f) — safety gate bypass is "
                "operator-explicit",
                version, target.brier_score, target.roc_auc,
            )

        # W23-7 — demote the current champion (if it is a different
        # version) and crown the rollback target as the new champion.
        # The previous champion's lifecycle state transitions to
        # ``demoted`` so it stays in the lineage for fast re-rollback but
        # is no longer considered the active production model.
        previous_record = next(
            (v for v in self.versions if v.version == previous),
            None,
        )
        if previous_record is not None and previous_record.state == MODEL_STATE_CHAMPION:
            previous_record.state = MODEL_STATE_DEMOTED
            log.info(
                "[model_registry] ⬇️ Previous champion %s demoted → demoted (rollback)",
                previous_record.version,
            )
        target.state = MODEL_STATE_CHAMPION

        self.active_version = version
        self._save_to_disk()
        log.info(
            "[model_registry] ⏪ Rolled back active_version: %s → %s "
            "(Brier=%.4f, AUC=%.4f, ECE=%.4f, Sharpe=%.2f, n_samples=%d, status=%s, state=%s)",
            previous, version,
            target.brier_score, target.roc_auc, target.ece,
            target.sharpe_ratio, target.n_samples, target.status, target.state,
        )
        return True

    # ── W23-7: Model version lifecycle management ─────────────────────────────
    # Promote / demote / state-inspection surface. Additive — the existing
    # ``register_version`` / ``list_versions`` / ``rollback`` / ``get_summary``
    # methods are extended (not replaced) with lifecycle-state transitions.
    # The new methods are consumed by the new ``ml/routes.py`` HTTP surface
    # (``GET /api/ml/lifecycle``, ``POST /api/ml/{version}/promote``,
    # ``POST /api/ml/{version}/rollback`` (lifecycle variant),
    # ``POST /api/ml/{version}/demote``).
    #
    # Lifecycle ladder:
    #
    #   experimental  →  shadow  →  challenger  →  champion
    #                                                  ↓
    #                                               demoted  →  retired
    #
    # Each ``promote(version)`` call advances the model one step along the
    # promotion ladder. ``demote(version)`` walks the demotion ladder.
    # ``rollback(version)`` (the T8 method above) ALSO performs the
    # ``*  →  champion`` transition on the target plus the
    # ``champion  →  demoted`` transition on the previous active champion,
    # so an operator-initiated rollback automatically updates the
    # lifecycle states without needing a separate ``set_state`` call.

    def get_active_version(self) -> str:
        """Return the currently active (champion) version string.

        Thin accessor over ``self.active_version`` — exposed so the
        lifecycle helpers (``promote`` / ``rollback`` / ``demote``) read
        the active version through a single method call rather than
        reaching into the instance attribute directly, and so external
        callers (``ml/routes.py`` HTTP surface) have a stable method-name
        contract independent of the underlying attribute name.

        Returns:
            str: The currently active version id. Matches the
            ``version`` field of the lifecycle-state-``champion`` record
            (or the baseline ``"v1.0.0"`` before any version has been
            registered).
        """
        return self.active_version

    def set_active(self, version: str) -> bool:
        """Re-point ``active_version`` at ``version`` and persist.

        Returns ``True`` if ``version`` is in the registry lineage (and
        ``active_version`` was therefore updated); ``False`` if the
        version is unknown (and the registry was left untouched).

        This is the **pointer-only** half of the rollback / promote
        contract — it does NOT touch lifecycle state. Callers that need
        the lifecycle-state side-effect (``champion → demoted`` on the
        previous active, ``* → champion`` on the new target) should use
        :meth:`rollback` or :meth:`promote` instead.
        """
        target = next((v for v in self.versions if v.version == version), None)
        if target is None:
            log.warning(
                "[model_registry] ❌ set_active FAILED: version %s not found",
                version,
            )
            return False
        self.active_version = version
        self._save_to_disk()
        return True

    def get_state(self, version: str) -> Optional[str]:
        """Return the lifecycle state of ``version``.

        Returns ``None`` if the version is not in the registry lineage
        (so callers can distinguish "version not found" from "version in
        the ``experimental`` state" — both of which would otherwise
        surface as a falsy string).
        """
        target = next((v for v in self.versions if v.version == version), None)
        return target.state if target is not None else None

    def set_state(self, version: str, state: str) -> bool:
        """Change a model's lifecycle state.

        Args:
            version: Target version string. Must already be in the
                registry lineage (``set_state`` cannot register a new
                version — use :meth:`register_version` for that).
            state: New lifecycle state. Should be one of the
                ``MODEL_STATE_*`` constants declared at module scope;
                ``set_state`` does NOT validate against this list so
                callers can introduce custom intermediate states if
                needed (defensive — a typo would otherwise crash an
                operator-driven promotion mid-flow).

        Returns:
            bool: ``True`` if the version was found and updated (the
            change is persisted via :meth:`_save_to_disk`); ``False`` if
            the version is not in the lineage (state is left untouched).
        """
        target = next((v for v in self.versions if v.version == version), None)
        if target is None:
            log.warning(
                "[model_registry] ❌ set_state FAILED: version %s not found",
                version,
            )
            return False
        previous_state = target.state
        target.state = state
        self._save_to_disk()
        log.info(
            "[model_registry] 🔄 Lifecycle state change: %s %s → %s",
            version, previous_state, state,
        )
        return True

    def promote(self, version: str) -> bool:
        """Promote a model along the lifecycle ladder.

        Each call advances the model ONE step along the promotion ladder:

            experimental  →  shadow  →  challenger  →  champion

        When the model reaches ``champion``:

        * The current champion (if any) is demoted to ``demoted`` (so
          there is at most ONE champion in the lineage at a time).
        * ``active_version`` is re-pointed at ``version``.
        * The target's ``state`` is set to ``champion``.

        Returns ``False`` (no-op) when:

        * ``version`` is not in the registry lineage (logged at WARNING).
        * The model is already ``champion`` (idempotency — re-promoting
          the champion would be a destructive no-op).
        * The model is ``demoted`` or ``retired`` (the demotion ladder
          is one-way; promote cannot reverse it — use :meth:`rollback`
          to re-crown a previously-demoted champion).

        Args:
            version: Target version string. Must already be in the
                registry lineage.

        Returns:
            bool: ``True`` if the model was promoted one step (state
            transition applied + persisted); ``False`` if the version
            was not found OR the model is already at a terminal state
            (``champion`` / ``demoted`` / ``retired``).
        """
        current = self.get_state(version)
        if current is None:
            log.warning(
                "[model_registry] ❌ promote FAILED: version %s not found",
                version,
            )
            return False

        if current == MODEL_STATE_EXPERIMENTAL:
            return self.set_state(version, MODEL_STATE_SHADOW)

        if current == MODEL_STATE_SHADOW:
            return self.set_state(version, MODEL_STATE_CHALLENGER)

        if current == MODEL_STATE_CHALLENGER:
            # Demote the current champion (if any) before crowning the
            # new one — at most ONE champion in the lineage at a time.
            previous_champion = next(
                (v for v in self.versions if v.state == MODEL_STATE_CHAMPION),
                None,
            )
            if previous_champion is not None and previous_champion.version != version:
                previous_champion.state = MODEL_STATE_DEMOTED
                log.info(
                    "[model_registry] ⬇️ Previous champion %s demoted → demoted (promote)",
                    previous_champion.version,
                )
            target = next((v for v in self.versions if v.version == version), None)
            target.state = MODEL_STATE_CHAMPION
            self.active_version = version
            self._save_to_disk()
            log.info(
                "[model_registry] 👑 Promoted %s → champion (active_version)",
                version,
            )
            return True

        # Already champion / demoted / retired — no-op (one-way ladder).
        log.info(
            "[model_registry] ℹ️ promote no-op: version %s is in state %s "
            "(no forward transition available)",
            version, current,
        )
        return False

    def demote(self, version: str) -> bool:
        """Demote a model along the lifecycle ladder.

        Each call advances the model ONE step along the demotion ladder:

            champion  →  demoted  →  retired

        When the active champion is demoted, ``active_version`` is left
        pointing at the demoted model — the registry's contract is that
        ``active_version`` identifies the production model, and demoting
        a champion does NOT automatically promote a successor (the
        operator must explicitly ``promote`` / ``rollback`` to re-crown
        a champion). This mirrors the existing ``rollback`` contract:
        the registry is a metadata store, not a model loader.

        Returns ``False`` (no-op) when:

        * ``version`` is not in the registry lineage (logged at WARNING).
        * The model is ``experimental`` / ``shadow`` / ``challenger``
          (those are pre-champion states; the demotion ladder starts at
          ``champion``).
        * The model is already ``retired`` (terminal state).

        Args:
            version: Target version string. Must already be in the
                registry lineage.

        Returns:
            bool: ``True`` if the model was demoted one step (state
            transition applied + persisted); ``False`` if the version
            was not found OR the model is already at a terminal /
            pre-champion state.
        """
        current = self.get_state(version)
        if current is None:
            log.warning(
                "[model_registry] ❌ demote FAILED: version %s not found",
                version,
            )
            return False

        if current == MODEL_STATE_CHAMPION:
            return self.set_state(version, MODEL_STATE_DEMOTED)

        if current == MODEL_STATE_DEMOTED:
            return self.set_state(version, MODEL_STATE_RETIRED)

        # experimental / shadow / challenger / retired — no-op (the
        # demotion ladder starts at champion and ends at retired).
        log.info(
            "[model_registry] ℹ️ demote no-op: version %s is in state %s "
            "(no downward transition available)",
            version, current,
        )
        return False

    def get_lifecycle(self) -> list[dict[str, Any]]:
        """Return all model versions with their lifecycle states, newest-first.

        Each entry carries the subset of fields an operator needs to
        assess the model fleet's lifecycle progression without pulling
        the full metric payload (the full payload is available via
        :meth:`list_versions`):

          * ``version``     — version string
          * ``state``       — lifecycle state (one of ``MODEL_STATE_*``)
          * ``status``      — safety-gate verdict (``ACTIVE`` / ``REJECTED``)
          * ``brier``       — Brier score (rounded to 4dp)
          * ``auc``         — ROC-AUC (rounded to 4dp)
          * ``n_samples``  — sample count the metrics were computed on
          * ``created_at`` — registration timestamp (Unix epoch seconds)
          * ``is_active``  — ``True`` iff this version is the current
            ``active_version`` (champion pointer)

        Returns:
            list[dict]: One dict per registered version, newest-first
            (the order in which :meth:`register_version` inserts). Never
            ``None``; empty only when the registry has never been seeded
            (which, by construction in :meth:`_load_from_disk`, cannot
            happen — the baseline ``v1.0.0`` is always present).
        """
        return [
            {
                "version": v.version,
                "state": v.state,
                "status": v.status,
                "brier": round(v.brier_score, 4),
                "auc": round(v.roc_auc, 4),
                "n_samples": v.n_samples,
                "created_at": v.created_at,
                "is_active": v.version == self.active_version,
            }
            for v in self.versions
        ]

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
                    # W23-7 — backward-compat: older registry JSON files
                    # lack the ``state`` field. Derive a sensible default
                    # from the existing fields:
                    #   * If this version is the active_version → champion
                    #     (it's the production model).
                    #   * Else if status=ACTIVE (passed safety gate but
                    #     is no longer active) → demoted (was promoted,
                    #     has since been superseded).
                    #   * Else (status=REJECTED) → experimental (failed
                    #     the safety gate, never promoted).
                    state=v.get(
                        "state",
                        MODEL_STATE_CHAMPION
                        if v["version"] == self.active_version
                        else (
                            MODEL_STATE_DEMOTED
                            if v.get("status") == "ACTIVE"
                            else MODEL_STATE_EXPERIMENTAL
                        ),
                    ),
                )
                for v in data.get("versions", [])
            ]
        except Exception as e:
            log.warning("[model_registry] Load error: %s", e)


# Global singleton
model_registry = ModelRegistry()
