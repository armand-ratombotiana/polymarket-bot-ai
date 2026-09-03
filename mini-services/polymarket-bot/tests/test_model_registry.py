"""
W9-5 — Unit tests for ``ml/model_registry.py``.

Covers the model-governance safety gate + version lineage surface:

  1. ``register_version`` with healthy benchmarks (Brier ≤ 0.22, AUC ≥ 0.70)
     is PROMOTED to ACTIVE — ``active_version`` updates + the new record
     appears at the front of ``versions``.
  2. ``register_version`` with Brier > 0.22 is REJECTED (safety gate) — the
     record is still stored, but ``active_version`` is NOT updated and
     ``status`` = ``"REJECTED"``.
  3. ``register_version`` with AUC < 0.70 is REJECTED (safety gate).
  4. ``register_version`` at the EXACT gate boundary (Brier=0.22, AUC=0.70)
     is PROMOTED — the gate is ``> 0.22 or < 0.70``, so the boundary value
     itself passes.
  5. ``list_versions`` returns the lineage newest-first, with the correct
     ``is_active`` flag on the currently-promoted row.
  6. ``list_versions`` flags ONLY ONE row as active (the currently promoted
     version) — never zero, never two.
  7. ``rollback(version)`` to a previously registered version returns ``True``
     and re-points ``active_version`` to it.
  8. ``rollback(unknown_version)`` returns ``False`` and leaves state untouched.
  9. ``rollback(version)`` to a REJECTED version is permitted (operator
     override) and still returns ``True`` — the safety-gate bypass is
     intentional, gated by the operator's explicit action.
 10. ``rollback(version)`` to the CURRENTLY active version is a no-op and
     returns ``True`` (idempotent).
 11. ``get_summary`` returns ``active_version`` + ``total_registered`` + the
     ``versions`` array.
 12. ``ModelVersionRecord.to_dict`` rounds the float metrics — brier_score /
     roc_auc / ece round to 4dp; sharpe_ratio rounds to 2dp.

Isolation
----------
The module-level singleton ``model_registry`` is constructed at import time
against the conftest-redirected ``MODEL_REGISTRY_PATH`` and seeded with the
default ``v1.0.0`` baseline. Each test constructs a FRESH ``ModelRegistry()``
instance to avoid perturbing the singleton's state across tests — but since
``_load_from_disk`` runs in ``__init__`` and will pick up whatever the
conftest-redirected ``REGISTRY_FILE`` contains at test time, we monkeypatch
``REGISTRY_FILE`` to a ``tmp_path``-scoped path so each test starts from an
empty baseline.

The fresh-instance ``__init__`` calls ``_load_from_disk`` which, on a missing
file, calls ``register_version("v1.0.0", ...)`` to seed the baseline. Each
test therefore starts with exactly one version (``v1.0.0``, ACTIVE).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module).
"""
from __future__ import annotations

import pytest

from ml.model_registry import ModelRegistry, ModelVersionRecord

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fresh_registry(monkeypatch, tmp_path):
    """Fresh ``ModelRegistry`` whose JSON file lives under ``tmp_path``.

    ``REGISTRY_FILE`` is monkeypatched so the no-arg ``ModelRegistry()`` ctor
    picks up the test path. The fresh instance's ``__init__`` calls
    ``_load_from_disk`` which, finding no file, seeds the default ``v1.0.0``
    baseline — exactly one version, marked ACTIVE.
    """
    registry_file = tmp_path / "test_model_registry.json"
    monkeypatch.setattr("ml.model_registry.REGISTRY_FILE", registry_file)
    return ModelRegistry()


# ── 1. Healthy benchmarks PROMOTE to ACTIVE ─────────────────────────────────
async def test_register_version_healthy_promotes_to_active(fresh_registry):
    """``register_version`` with Brier ≤ 0.22 AND AUC ≥ 0.70 must promote the
    new version to ACTIVE — ``active_version`` updates and the new record
    appears at the front of ``versions``."""
    promoted = fresh_registry.register_version(
        version="v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={"n_estimators": 100},
    )
    assert promoted is True
    assert fresh_registry.active_version == "v1.1.0"
    # New version is at the FRONT of the list (newest-first insertion order).
    assert fresh_registry.versions[0].version == "v1.1.0"
    assert fresh_registry.versions[0].status == "ACTIVE"


# ── 2. Brier > 0.22 is REJECTED (safety gate) ────────────────────────────────
async def test_register_version_high_brier_is_rejected(fresh_registry):
    """Brier > 0.22 trips the safety gate — the record is still stored (so
    the lineage is observable) but ``active_version`` is NOT updated and
    ``status`` = ``"REJECTED"``."""
    previous_active = fresh_registry.active_version
    promoted = fresh_registry.register_version(
        version="v1.2.0",
        brier_score=0.25,  # > 0.22 gate
        roc_auc=0.85,
        ece=0.05,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    assert promoted is False
    # Active version was NOT updated.
    assert fresh_registry.active_version == previous_active
    # Record was still stored, with REJECTED status, at the front of the list.
    assert fresh_registry.versions[0].version == "v1.2.0"
    assert fresh_registry.versions[0].status == "REJECTED"


# ── 3. AUC < 0.70 is REJECTED (safety gate) ─────────────────────────────────
async def test_register_version_low_auc_is_rejected(fresh_registry):
    """AUC < 0.70 trips the safety gate — record stored, no promotion."""
    previous_active = fresh_registry.active_version
    promoted = fresh_registry.register_version(
        version="v1.3.0",
        brier_score=0.18,  # passes brier gate
        roc_auc=0.65,  # < 0.70 gate
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    assert promoted is False
    assert fresh_registry.active_version == previous_active
    assert fresh_registry.versions[0].status == "REJECTED"


# ── 4. Boundary values (Brier=0.22, AUC=0.70) are PROMOTED ───────────────────
async def test_register_version_boundary_values_are_promoted(fresh_registry):
    """The safety gate is ``brier > 0.22 or roc_auc < 0.70``. Values AT the
    boundary (0.22 exactly, 0.70 exactly) must PASS — the comparison is
    strictly greater / strictly less, not ≥ / ≤."""
    promoted = fresh_registry.register_version(
        version="v1.boundary.0",
        brier_score=0.22,  # exactly at the gate (not > 0.22)
        roc_auc=0.70,  # exactly at the gate (not < 0.70)
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    assert promoted is True
    assert fresh_registry.active_version == "v1.boundary.0"
    assert fresh_registry.versions[0].status == "ACTIVE"


# ── 5. list_versions returns newest-first with is_active flag ────────────────
async def test_list_versions_returns_newest_first_with_is_active_flag(fresh_registry):
    """``list_versions`` returns the full lineage newest-first. The currently
    promoted row must carry ``is_active=True``; every other row must carry
    ``is_active=False``."""
    fresh_registry.register_version(
        "v1.1.0", brier_score=0.18, roc_auc=0.80, ece=0.04,
        sharpe_ratio=1.5, n_samples=2000, parameters={},
    )
    fresh_registry.register_version(
        "v1.2.0", brier_score=0.17, roc_auc=0.82, ece=0.03,
        sharpe_ratio=1.6, n_samples=3000, parameters={},
    )

    out = fresh_registry.list_versions()
    assert len(out) == 3  # v1.0.0 baseline + v1.1.0 + v1.2.0

    # Newest-first: v1.2.0, then v1.1.0, then v1.0.0 baseline.
    assert [v["version"] for v in out] == ["v1.2.0", "v1.1.0", "v1.0.0"]

    # Exactly one row is_active=True (the most recently promoted version).
    active_rows = [v for v in out if v["is_active"] is True]
    assert len(active_rows) == 1
    assert active_rows[0]["version"] == "v1.2.0"

    # The two non-active rows are flagged False.
    inactive_rows = [v for v in out if v["is_active"] is False]
    assert len(inactive_rows) == 2


# ── 6. list_versions flags EXACTLY one active row ───────────────────────────
async def test_list_versions_flags_exactly_one_active_row(fresh_registry):
    """Even when the most recent registration was REJECTED, ``list_versions``
    must flag exactly ONE row as active — the previously-promoted ACTIVE row
    (the active_version pointer was not updated by the rejected registration)."""
    # Baseline v1.0.0 is ACTIVE. Register a REJECTED version on top.
    fresh_registry.register_version(
        "v1.rejected.0", brier_score=0.30, roc_auc=0.60,  # both gates fail
        ece=0.10, sharpe_ratio=0.5, n_samples=100, parameters={},
    )

    out = fresh_registry.list_versions()
    assert len(out) == 2

    active_rows = [v for v in out if v["is_active"] is True]
    assert len(active_rows) == 1
    # The active row is still v1.0.0 (the rejected registration did not
    # promote v1.rejected.0).
    assert active_rows[0]["version"] == "v1.0.0"
    assert active_rows[0]["status"] == "ACTIVE"


# ── 7. rollback to a previously registered version returns True ─────────────
async def test_rollback_to_registered_version_returns_true(fresh_registry):
    """``rollback(version)`` for a version in the lineage must return True and
    re-point ``active_version`` to it."""
    fresh_registry.register_version(
        "v1.1.0", brier_score=0.18, roc_auc=0.80, ece=0.04,
        sharpe_ratio=1.5, n_samples=2000, parameters={},
    )
    fresh_registry.register_version(
        "v1.2.0", brier_score=0.17, roc_auc=0.82, ece=0.03,
        sharpe_ratio=1.6, n_samples=3000, parameters={},
    )
    # Active is now v1.2.0. Roll back to v1.1.0.
    ok = fresh_registry.rollback("v1.1.0")
    assert ok is True
    assert fresh_registry.active_version == "v1.1.0"

    # list_versions reflects the new active flag.
    out = fresh_registry.list_versions()
    active_rows = [v for v in out if v["is_active"] is True]
    assert len(active_rows) == 1
    assert active_rows[0]["version"] == "v1.1.0"


# ── 8. rollback to unknown version returns False ────────────────────────────
async def test_rollback_to_unknown_version_returns_false(fresh_registry):
    """``rollback(unknown)`` returns False and leaves ALL state untouched
    (``active_version`` + ``versions`` list)."""
    previous_active = fresh_registry.active_version
    previous_count = len(fresh_registry.versions)

    ok = fresh_registry.rollback("v9.999.does_not_exist")
    assert ok is False
    assert fresh_registry.active_version == previous_active
    assert len(fresh_registry.versions) == previous_count


# ── 9. rollback to a REJECTED version is permitted (operator override) ──────
async def test_rollback_to_rejected_version_is_permitted(fresh_registry):
    """A REJECTED version can still be rolled back to — the safety gate
    blocks AUTOMATIC promotion but a human-in-the-loop explicit rollback
    must succeed (the documented operator-override contract)."""
    # Register a rejected version on top of the v1.0.0 baseline.
    fresh_registry.register_version(
        "v1.rejected.0", brier_score=0.30, roc_auc=0.60,
        ece=0.10, sharpe_ratio=0.5, n_samples=100, parameters={},
    )
    assert fresh_registry.active_version == "v1.0.0"

    # Operator-explicit rollback to the rejected version.
    ok = fresh_registry.rollback("v1.rejected.0")
    assert ok is True
    assert fresh_registry.active_version == "v1.rejected.0"

    # list_versions now flags the rejected version as active.
    out = fresh_registry.list_versions()
    active_rows = [v for v in out if v["is_active"] is True]
    assert len(active_rows) == 1
    assert active_rows[0]["version"] == "v1.rejected.0"
    # The status of the rolled-back version is unchanged (still REJECTED).
    assert active_rows[0]["status"] == "REJECTED"


# ── 10. rollback to the CURRENTLY active version is a no-op ──────────────────
async def test_rollback_to_currently_active_version_is_noop(fresh_registry):
    """``rollback(active_version)`` is a no-op — returns True and leaves
    state untouched. This is the idempotency contract."""
    current = fresh_registry.active_version
    ok = fresh_registry.rollback(current)
    assert ok is True
    assert fresh_registry.active_version == current


# ── 11. get_summary returns active_version + total + versions ─────────────────
async def test_get_summary_returns_active_version_total_and_versions(fresh_registry):
    """``get_summary`` must surface ``active_version``, ``total_registered``,
    and ``versions`` (each a dict)."""
    fresh_registry.register_version(
        "v1.1.0", brier_score=0.18, roc_auc=0.80, ece=0.04,
        sharpe_ratio=1.5, n_samples=2000, parameters={"n": 1},
    )
    summary = fresh_registry.get_summary()

    assert summary["active_version"] == "v1.1.0"
    assert summary["total_registered"] == 2  # baseline + v1.1.0
    assert isinstance(summary["versions"], list)
    assert len(summary["versions"]) == 2
    assert summary["versions"][0]["version"] == "v1.1.0"
    assert summary["versions"][1]["version"] == "v1.0.0"


# ── 12. ModelVersionRecord.to_dict rounds float metrics ──────────────────────
async def test_model_version_record_to_dict_rounds_float_metrics():
    """``to_dict`` must round Brier/AUC/ECE to 4dp and Sharpe to 2dp — the
    exact precision contract published by the API surface."""
    record = ModelVersionRecord(
        version="v1.test",
        created_at=1234567890.0,
        brier_score=0.183856789,  # 5dp after the 4dp round
        roc_auc=0.793951234,
        ece=0.0385112,
        sharpe_ratio=1.923456,  # 3dp after the 2dp round
        status="ACTIVE",
        n_samples=3000,
        parameters={"a": 1},
    )
    out = record.to_dict()

    assert out["version"] == "v1.test"
    assert out["brier_score"] == 0.1839  # 4dp
    assert out["roc_auc"] == 0.7940  # 4dp
    assert out["ece"] == 0.0385  # 4dp
    assert out["sharpe_ratio"] == 1.92  # 2dp
    assert out["status"] == "ACTIVE"
    assert out["n_samples"] == 3000
    assert out["parameters"] == {"a": 1}
    assert out["created_at"] == 1234567890.0
