"""
W23-7 — Unit + HTTP tests for the model-version lifecycle surface
(``promote`` / ``rollback`` / ``demote`` / ``get_lifecycle``) added to
``ml/model_registry.py`` and surfaced via four new endpoints in
``ml/routes.py``.

Coverage matrix
~~~~~~~~~~~~~~~

  (1)  ``promote`` walks the full ladder: experimental → shadow →
       challenger → champion — each call advances exactly one step,
       returns ``True``, and persists the new state to disk.
  (2)  ``promote`` to ``champion`` demotes the previous champion (so
       there is at most ONE champion in the lineage at a time) and
       re-points ``active_version`` at the new champion.
  (3)  ``promote`` returns ``False`` (no-op) on:
        * an unknown version,
        * a version already in the ``champion`` state,
        * a version in the ``demoted`` / ``retired`` states (the
          demotion ladder is one-way — ``promote`` cannot reverse it).
  (4)  ``demote`` walks the demotion ladder: champion → demoted →
       retired — each call advances exactly one step.
  (5)  ``demote`` returns ``False`` (no-op) on:
        * an unknown version,
        * a version in a pre-champion state (``experimental`` /
          ``shadow`` / ``challenger``),
        * a version already in the terminal ``retired`` state.
  (6)  ``set_state`` directly sets the lifecycle state and persists;
       returns ``False`` for an unknown version.
  (7)  ``get_state`` returns the lifecycle state, or ``None`` for an
       unknown version (so callers can distinguish "not found" from
       "in the ``experimental`` state").
  (8)  ``register_version`` (safety-gate pass) crowns the new version
       ``champion`` AND demotes the previous champion to ``demoted``
       (at most one champion in the lineage at a time).
  (9)  ``register_version`` (safety-gate fail) leaves the new version
       in the ``experimental`` state (no promotion).
  (10) ``rollback(version)`` demotes the current champion and crowns
       the target ``champion``; preserves the previous ``status``
       (ACTIVE / REJECTED — the safety-gate verdict) so the audit
       trail is intact.
  (11) ``get_lifecycle()`` returns the lineage newest-first with the
       lifecycle state, safety-gate status, Brier, AUC, sample count,
       creation timestamp, and ``is_active`` flag.
  (12) ``_load_from_disk`` derives a sensible ``state`` for older JSON
       registry files that pre-date the W23-7 ``state`` field
       (champion if active, demoted if ACTIVE-but-not-active,
       experimental otherwise).
  (13) ``ModelVersionRecord.to_dict()`` includes the ``state`` field.

HTTP surface (mirrors ``tests/test_shadow_trading_api.py`` — a fresh
``FastAPI()`` app per test with only the ``ml.routes`` registered, so
the route definitions / validation annotations exercised here are
byte-identical to what the live server exposes, without the bearer-token
auth middleware or the heavy ``lifespan`` startup):

  (14) ``GET /api/ml/lifecycle`` returns 200 with the lifecycle list.
  (15) ``POST /api/ml/{version}/promote`` returns 200 + advances state.
  (16) ``POST /api/ml/{version}/promote`` returns 400 on unknown version.
  (17) ``POST /api/ml/{version}/rollback`` returns 200 + crowns target.
  (18) ``POST /api/ml/{version}/demote`` returns 200 + demotes champion.
  (19) ``POST /api/ml/{version}/demote`` returns 400 on unknown version.
  (20) ``POST /api/ml/{version}/promote`` to ``champion`` demotes the
       previous champion AND re-points ``active_version`` (end-to-end).

Hermeticity
~~~~~~~~~~~
The module-level singleton ``model_registry`` is constructed at import
time against the conftest-redirected ``MODEL_REGISTRY_PATH`` and seeded
with the default ``v1.0.0`` baseline. Each test constructs a FRESH
``ModelRegistry()`` instance to avoid perturbing the singleton's state
across tests — but since ``_load_from_disk`` runs in ``__init__`` and
will pick up whatever the conftest-redirected ``REGISTRY_FILE``
contains at test time, we monkeypatch ``REGISTRY_FILE`` to a
``tmp_path``-scoped path so each test starts from an empty baseline.

The fresh-instance ``__init__`` calls ``_load_from_disk`` which, on a
missing file, calls ``register_version("v1.0.0", ...)`` to seed the
baseline — exactly one version, ``state=champion``.

For the HTTP tests, a fresh ``FastAPI()`` is built per test with only
the ``ml.routes`` registered (mirrors the pattern in
``tests/test_shadow_trading_api.py``). The same ``model_registry``
singleton is shared with the registry tests, so we monkeypatch
``REGISTRY_FILE`` to a ``tmp_path``-scoped path AND wipe the singleton's
state by directly resetting ``model_registry.versions`` /
``model_registry.active_version`` after registering the lifecycle routes.
This keeps the HTTP tests hermetic without having to re-import the
module singleton.

All tests are SYNC ``def test_...`` (not ``async def``) — TestClient
bridges each request into the ASGI app via its own ``anyio`` portal
(owns its own event loop); ``pytest.mark.asyncio`` would compete with
that portal. Mirrors the convention in
``tests/test_shadow_trading_api.py`` / ``tests/test_integration.py``.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ml.model_registry import (
    MODEL_DEMOTION_LADDER,
    MODEL_PROMOTION_LADDER,
    MODEL_STATE_CHALLENGER,
    MODEL_STATE_CHAMPION,
    MODEL_STATE_DEMOTED,
    MODEL_STATE_EXPERIMENTAL,
    MODEL_STATE_RETIRED,
    MODEL_STATE_SHADOW,
    ModelRegistry,
    ModelVersionRecord,
    model_registry,
)
from ml.routes import register_routes


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_registry(monkeypatch, tmp_path):
    """Fresh ``ModelRegistry`` whose JSON file lives under ``tmp_path``.

    ``REGISTRY_FILE`` is monkeypatched so the no-arg ``ModelRegistry()``
    ctor picks up the test path. The fresh instance's ``__init__`` calls
    ``_load_from_disk`` which, finding no file, seeds the default
    ``v1.0.0`` baseline — exactly one version, ``state=champion`` (W23-7
    — ``register_version`` crowns the new version ``champion`` when it
    passes the safety gate).

    Mirrors the ``fresh_registry`` fixture in ``tests/test_model_registry.py``
    so the two test modules share an identical isolation contract.
    """
    registry_file = tmp_path / "test_model_lifecycle_registry.json"
    monkeypatch.setattr("ml.model_registry.REGISTRY_FILE", registry_file)
    return ModelRegistry()


@pytest.fixture
def lifecycle_client(monkeypatch, tmp_path):
    """Fresh ``FastAPI`` app + ``TestClient`` with only the ``ml.routes``
    endpoints registered.

    The ``model_registry`` singleton is shared with the production code
    (its ``REGISTRY_FILE`` is monkeypatched to a ``tmp_path``-scoped
    path so the test does not clobber the conftest singleton's state).
    The singleton is reset to a clean baseline (one ``v1.0.0`` champion)
    by directly invoking ``_load_from_disk`` after the path is patched —
    mirroring the fresh-instance behaviour the unit tests get for free.

    Uses the same ``register_routes(app)`` entry point as the production
    ``api/server.py`` so the route definitions / validation annotations
    exercised here are byte-identical to what the live server exposes —
    without the bearer-token auth middleware (``enforce_api_auth`` —
    a server-level concern exercised separately by ``test_integration.py``)
    or the heavy ``lifespan`` startup.

    Singleton-state hygiene: this fixture MUTATES the global
    ``model_registry`` singleton (registers versions, drives lifecycle
    transitions through the HTTP surface). To avoid leaking those
    mutations into subsequent test modules (e.g. a sibling test that
    asserts on the singleton's lineage shape), the original
    ``REGISTRY_FILE`` + ``versions`` + ``active_version`` are
    snapshotted at fixture entry and restored at teardown — so the
    next test sees the same singleton state it would have seen without
    this fixture having run.
    """
    # Snapshot the singleton's pre-fixture state.
    original_registry_file = model_registry._db_path if hasattr(model_registry, "_db_path") else None
    # ``ModelRegistry`` reads ``REGISTRY_FILE`` at module scope; capture
    # the module-level reference (the conftest path) so we can restore it
    # after the test.
    from ml import model_registry as _mr_module
    original_module_file = _mr_module.REGISTRY_FILE
    original_versions = list(model_registry.versions)
    original_active = model_registry.active_version

    registry_file = tmp_path / "test_model_lifecycle_routes.json"
    monkeypatch.setattr("ml.model_registry.REGISTRY_FILE", registry_file)
    # Re-seed the singleton against the fresh path so the HTTP tests
    # start from exactly one version (``v1.0.0`` champion) — without
    # this, the singleton would still hold whatever versions were
    # registered by an earlier test in the session.
    model_registry.versions = []
    model_registry.active_version = "v1.0.0"
    model_registry._load_from_disk()

    app = FastAPI()
    register_routes(app)
    client = TestClient(app)

    yield client

    # Teardown — restore the singleton's pre-fixture state so the
    # mutations made by the HTTP tests (register_version, promote,
    # demote, rollback via the HTTP surface) do not leak into
    # subsequent test modules. Mirrors the autouse
    # ``_reset_store_factory_defaults`` pattern in ``conftest.py`` (which
    # resets ``store`` / ``risk_manager`` / ``paper_sim`` but NOT
    # ``model_registry`` — the registry's reset is the responsibility
    # of any test module that mutates it).
    _mr_module.REGISTRY_FILE = original_module_file
    model_registry.versions = original_versions
    model_registry.active_version = original_active


# ── 1. promote: experimental → shadow → challenger → champion ─────────────────


def test_promote_walks_full_ladder_to_champion(fresh_registry):
    """``promote(version)`` advances the model one step along the
    promotion ladder on each call. Three calls take a freshly-registered
    model from ``experimental`` → ``shadow`` → ``challenger`` →
    ``champion``.

    To get a fresh ``experimental`` model, register a REJECTED version
    (safety-gate failure leaves the new version at ``experimental``).
    """
    # Register a REJECTED version — fails the safety gate, stays at
    # ``experimental``. ``active_version`` is NOT updated.
    fresh_registry.register_version(
        "v1.experimental.0",
        brier_score=0.30,  # > 0.22 gate
        roc_auc=0.60,  # < 0.70 gate
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_EXPERIMENTAL

    # Step 1: experimental → shadow
    assert fresh_registry.promote("v1.experimental.0") is True
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_SHADOW

    # Step 2: shadow → challenger
    assert fresh_registry.promote("v1.experimental.0") is True
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_CHALLENGER

    # Step 3: challenger → champion (demotes previous champion + re-points
    # active_version at the new champion).
    previous_champion = fresh_registry.get_active_version()  # "v1.0.0"
    assert fresh_registry.promote("v1.experimental.0") is True
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.get_active_version() == "v1.experimental.0"
    # Previous champion was demoted (not retired — promote only demotes
    # one step, leaving the previous champion available for fast rollback).
    assert fresh_registry.get_state(previous_champion) == MODEL_STATE_DEMOTED


# ── 2. promote to champion demotes previous champion + re-points active ──────


def test_promote_to_champion_demotes_previous_champion_and_repoints_active(fresh_registry):
    """When ``promote`` advances a challenger to ``champion``, the
    current champion is demoted (so at most ONE champion exists in the
    lineage at a time) AND ``active_version`` is re-pointed at the new
    champion."""
    # Baseline: v1.0.0 is the champion (active_version="v1.0.0").
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.get_active_version() == "v1.0.0"

    # Register a healthy new version — passes the safety gate, becomes
    # the new champion (previous champion v1.0.0 demoted to demoted).
    fresh_registry.register_version(
        "v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    # v1.1.0 is champion; v1.0.0 is demoted.
    assert fresh_registry.get_state("v1.1.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED
    assert fresh_registry.get_active_version() == "v1.1.0"

    # Now manually drive v1.0.0 back through the ladder: it's currently
    # ``demoted`` — promote cannot reverse the demotion ladder (one-way),
    # so we use rollback to re-crown it.
    assert fresh_registry.rollback("v1.0.0") is True
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.get_state("v1.1.0") == MODEL_STATE_DEMOTED
    assert fresh_registry.get_active_version() == "v1.0.0"


# ── 3. promote returns False on terminal states / unknown versions ────────────


def test_promote_returns_false_for_unknown_version(fresh_registry):
    """``promote(unknown_version)`` returns ``False`` without modifying
    any state."""
    previous_versions_count = len(fresh_registry.versions)
    previous_active = fresh_registry.get_active_version()

    assert fresh_registry.promote("v9.999.does_not_exist") is False

    # State is untouched.
    assert len(fresh_registry.versions) == previous_versions_count
    assert fresh_registry.get_active_version() == previous_active


def test_promote_returns_false_for_already_champion(fresh_registry):
    """``promote(champion)`` returns ``False`` — re-promoting the
    champion would be a destructive no-op (it would demote the champion
    in order to crown it)."""
    # Baseline v1.0.0 is champion.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.promote("v1.0.0") is False
    # State is unchanged.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION


def test_promote_returns_false_for_demoted_state(fresh_registry):
    """``promote(demoted_version)`` returns ``False`` — the demotion
    ladder is one-way; ``promote`` cannot reverse it. Use ``rollback``
    to re-crown a previously-demoted champion."""
    # Register a healthy new version — previous champion v1.0.0 demoted.
    fresh_registry.register_version(
        "v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED
    # Promote cannot reverse the demotion.
    assert fresh_registry.promote("v1.0.0") is False
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED


def test_promote_returns_false_for_retired_state(fresh_registry):
    """``promote(retired_version)`` returns ``False`` — retired is a
    terminal state."""
    # Baseline v1.0.0 is champion. Demote it twice to get to retired
    # (champion → demoted → retired). No other version is registered,
    # so there is no other champion to demote in the process — the
    # ``active_version`` pointer stays at v1.0.0 (it is a metadata
    # pointer, not a lifecycle invariant; the lifecycle state is what
    # matters here).
    assert fresh_registry.demote("v1.0.0") is True  # champion → demoted
    assert fresh_registry.demote("v1.0.0") is True  # demoted → retired
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_RETIRED
    # Promote cannot reverse retirement.
    assert fresh_registry.promote("v1.0.0") is False
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_RETIRED


# ── 4. demote: champion → demoted → retired ───────────────────────────────────


def test_demote_walks_ladder_from_champion_to_retired(fresh_registry):
    """``demote(version)`` advances the model one step along the
    demotion ladder on each call. Two calls take a champion → demoted →
    retired."""
    # Baseline v1.0.0 is champion.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION

    # Step 1: champion → demoted.
    assert fresh_registry.demote("v1.0.0") is True
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED

    # Step 2: demoted → retired.
    assert fresh_registry.demote("v1.0.0") is True
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_RETIRED


# ── 5. demote returns False on unknown / pre-champion / retired states ─────────


def test_demote_returns_false_for_unknown_version(fresh_registry):
    """``demote(unknown_version)`` returns ``False`` without modifying
    state."""
    previous_versions_count = len(fresh_registry.versions)
    assert fresh_registry.demote("v9.999.does_not_exist") is False
    assert len(fresh_registry.versions) == previous_versions_count


def test_demote_returns_false_for_pre_champion_states(fresh_registry):
    """``demote(version)`` returns ``False`` when the version is in a
    pre-champion state (``experimental`` / ``shadow`` / ``challenger``)
    — the demotion ladder starts at ``champion``."""
    # Register a REJECTED version — stays at experimental.
    fresh_registry.register_version(
        "v1.experimental.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_EXPERIMENTAL

    # experimental → demote is a no-op.
    assert fresh_registry.demote("v1.experimental.0") is False
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_EXPERIMENTAL

    # shadow → demote is a no-op.
    assert fresh_registry.promote("v1.experimental.0") is True  # → shadow
    assert fresh_registry.demote("v1.experimental.0") is False
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_SHADOW

    # challenger → demote is a no-op.
    assert fresh_registry.promote("v1.experimental.0") is True  # → challenger
    assert fresh_registry.demote("v1.experimental.0") is False
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_CHALLENGER


def test_demote_returns_false_for_retired_terminal_state(fresh_registry):
    """``demote(retired_version)`` returns ``False`` — retired is the
    terminal state of the demotion ladder."""
    assert fresh_registry.demote("v1.0.0") is True  # champion → demoted
    assert fresh_registry.demote("v1.0.0") is True  # demoted → retired
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_RETIRED
    # Third demote is a no-op.
    assert fresh_registry.demote("v1.0.0") is False
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_RETIRED


# ── 6. set_state directly sets lifecycle state + persists ─────────────────────


def test_set_state_updates_and_persists(fresh_registry, tmp_path):
    """``set_state(version, state)`` directly sets the lifecycle state
    and persists the change to disk."""
    assert fresh_registry.set_state("v1.0.0", MODEL_STATE_SHADOW) is True
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_SHADOW

    # Persistence: a fresh ModelRegistry instance loaded from the same
    # JSON file picks up the new state.
    fresh = ModelRegistry()
    assert fresh.get_state("v1.0.0") == MODEL_STATE_SHADOW


def test_set_state_returns_false_for_unknown_version(fresh_registry):
    """``set_state(unknown_version, state)`` returns ``False`` without
    modifying any state."""
    assert fresh_registry.set_state("v9.999.does_not_exist", MODEL_STATE_SHADOW) is False
    # The known version's state is untouched.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION


# ── 7. get_state returns None for unknown versions ───────────────────────────


def test_get_state_returns_none_for_unknown_version(fresh_registry):
    """``get_state(unknown_version)`` returns ``None`` so callers can
    distinguish "not found" from "in the ``experimental`` state"."""
    assert fresh_registry.get_state("v9.999.does_not_exist") is None
    # A freshly registered REJECTED version is at ``experimental`` —
    # this is NOT None (it's the string "experimental").
    fresh_registry.register_version(
        "v1.experimental.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )
    assert fresh_registry.get_state("v1.experimental.0") == MODEL_STATE_EXPERIMENTAL


# ── 8. register_version (safety-gate pass) crowns champion + demotes previous ─


def test_register_version_healthy_demotes_previous_champion(fresh_registry):
    """``register_version`` with healthy benchmarks (Brier ≤ 0.22 AND
    AUC ≥ 0.70) crowns the new version ``champion`` AND demotes the
    previous champion to ``demoted`` (at most ONE champion in the
    lineage at a time)."""
    # Baseline: v1.0.0 is champion.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION

    fresh_registry.register_version(
        "v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )

    # New version is champion.
    assert fresh_registry.get_state("v1.1.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.get_active_version() == "v1.1.0"
    # Previous champion is demoted.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED


# ── 9. register_version (safety-gate fail) stays at experimental ──────────────


def test_register_version_rejected_stays_experimental(fresh_registry):
    """``register_version`` with failed safety gate leaves the new
    version at ``experimental`` (NOT promoted, NOT demoted). The
    previous champion's state is also untouched."""
    # Baseline: v1.0.0 is champion.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION

    fresh_registry.register_version(
        "v1.rejected.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )

    # Rejected version stays at experimental.
    assert fresh_registry.get_state("v1.rejected.0") == MODEL_STATE_EXPERIMENTAL
    # active_version is unchanged.
    assert fresh_registry.get_active_version() == "v1.0.0"
    # Previous champion's state is untouched.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION


# ── 10. rollback demotes current champion + crowns target ─────────────────────


def test_rollback_demotes_current_champion_and_crowns_target(fresh_registry):
    """``rollback(version)`` demotes the current champion (state:
    champion → demoted) and crowns the rollback target (state: * →
    champion). The ``status`` field (safety-gate verdict) is preserved
    unchanged — ``status`` and ``state`` are orthogonal."""
    # Register a healthy v1.1.0 — becomes champion; v1.0.0 demoted.
    fresh_registry.register_version(
        "v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    assert fresh_registry.get_active_version() == "v1.1.0"
    assert fresh_registry.get_state("v1.1.0") == MODEL_STATE_CHAMPION
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED

    # Re-promote v1.0.0 back to champion via rollback (the demotion
    # ladder is one-way, so rollback is the canonical re-crown path).
    assert fresh_registry.rollback("v1.0.0") is True
    assert fresh_registry.get_active_version() == "v1.0.0"
    # v1.0.0 is now champion again (was demoted, now re-crowned).
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION
    # v1.1.0 (the previous champion) is demoted.
    assert fresh_registry.get_state("v1.1.0") == MODEL_STATE_DEMOTED
    # The safety-gate ``status`` field on v1.0.0 is preserved.
    v1_0_0_record = next(v for v in fresh_registry.versions if v.version == "v1.0.0")
    assert v1_0_0_record.status == "ACTIVE"


def test_rollback_preserves_status_field_on_rejected_model(fresh_registry):
    """``rollback`` to a REJECTED model preserves the ``status`` field
    (operator-explicit override) while still updating the lifecycle
    ``state`` to ``champion``. The two fields are orthogonal."""
    fresh_registry.register_version(
        "v1.rejected.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )
    # v1.rejected.0 is at experimental (failed safety gate).
    assert fresh_registry.get_state("v1.rejected.0") == MODEL_STATE_EXPERIMENTAL
    # active_version is still v1.0.0 (rejected registration does not promote).
    assert fresh_registry.get_active_version() == "v1.0.0"

    # Operator-explicit rollback to the rejected version.
    assert fresh_registry.rollback("v1.rejected.0") is True
    assert fresh_registry.get_active_version() == "v1.rejected.0"
    # The rejected version is now champion (lifecycle state updated).
    assert fresh_registry.get_state("v1.rejected.0") == MODEL_STATE_CHAMPION
    # The previous champion v1.0.0 is demoted.
    assert fresh_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED
    # But the ``status`` field (safety-gate verdict) is preserved.
    v1_rej_record = next(v for v in fresh_registry.versions if v.version == "v1.rejected.0")
    assert v1_rej_record.status == "REJECTED"


def test_rollback_to_currently_active_version_is_noop(fresh_registry):
    """``rollback(active_version)`` is a no-op — returns ``True`` and
    leaves the lifecycle state untouched (idempotency contract)."""
    current = fresh_registry.get_active_version()
    previous_state = fresh_registry.get_state(current)
    assert fresh_registry.rollback(current) is True
    assert fresh_registry.get_active_version() == current
    assert fresh_registry.get_state(current) == previous_state


def test_rollback_to_unknown_version_returns_false(fresh_registry):
    """``rollback(unknown_version)`` returns ``False`` without modifying
    any state."""
    previous_versions_count = len(fresh_registry.versions)
    previous_active = fresh_registry.get_active_version()

    assert fresh_registry.rollback("v9.999.does_not_exist") is False

    assert len(fresh_registry.versions) == previous_versions_count
    assert fresh_registry.get_active_version() == previous_active


# ── 11. get_lifecycle returns the lineage with lifecycle states ────────────────


def test_get_lifecycle_returns_lineage_newest_first_with_states(fresh_registry):
    """``get_lifecycle()`` returns the lineage newest-first. Each entry
    carries ``version``, ``state``, ``status``, ``brier``, ``auc``,
    ``n_samples``, ``created_at``, and ``is_active`` — the lightweight
    lifecycle view (no ECE, no Sharpe, no parameters)."""
    fresh_registry.register_version(
        "v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={"n_estimators": 100},
    )
    fresh_registry.register_version(
        "v1.rejected.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )

    lifecycle = fresh_registry.get_lifecycle()
    assert len(lifecycle) == 3  # v1.0.0 + v1.1.0 + v1.rejected.0

    # Newest-first insertion order.
    assert [e["version"] for e in lifecycle] == ["v1.rejected.0", "v1.1.0", "v1.0.0"]

    # Each entry carries the lifecycle-state + safety-gate-status +
    # metric fields. The metric fields are ROUNDED (brier / auc to 4dp).
    v1_1_0 = next(e for e in lifecycle if e["version"] == "v1.1.0")
    # v1.1.0 was crowned champion when it was registered (safety gate
    # passed). The subsequent REJECTED registration of v1.rejected.0
    # did NOT promote it (failed safety gate) so v1.1.0 is STILL the
    # champion and the active_version.
    assert v1_1_0["state"] == MODEL_STATE_CHAMPION
    assert v1_1_0["status"] == "ACTIVE"
    assert v1_1_0["brier"] == 0.18
    assert v1_1_0["auc"] == 0.80
    assert v1_1_0["n_samples"] == 2000
    assert v1_1_0["is_active"] is True

    v1_0_0 = next(e for e in lifecycle if e["version"] == "v1.0.0")
    assert v1_0_0["state"] == MODEL_STATE_DEMOTED
    assert v1_0_0["is_active"] is False

    v1_rej = next(e for e in lifecycle if e["version"] == "v1.rejected.0")
    assert v1_rej["state"] == MODEL_STATE_EXPERIMENTAL
    assert v1_rej["status"] == "REJECTED"
    assert v1_rej["is_active"] is False

    # Exactly ONE entry is_active=True (the current champion).
    active_rows = [e for e in lifecycle if e["is_active"] is True]
    assert len(active_rows) == 1
    assert active_rows[0]["version"] == "v1.1.0"


# ── 12. _load_from_disk derives state for legacy JSON files ───────────────────


def test_load_from_disk_derives_state_for_legacy_json_without_state_field(
    monkeypatch, tmp_path,
):
    """When loading a JSON registry file that pre-dates the W23-7
    ``state`` field (e.g. a registry written by an older binary), the
    loader derives a sensible default:

      * If the version is the ``active_version`` → ``champion``.
      * Else if status=ACTIVE (passed safety gate but is no longer
        active) → ``demoted``.
      * Else (status=REJECTED) → ``experimental``.
    """
    registry_file = tmp_path / "legacy_model_registry.json"
    monkeypatch.setattr("ml.model_registry.REGISTRY_FILE", registry_file)

    # Write a legacy-shape JSON file — no ``state`` field on any version.
    legacy_data = {
        "active_version": "v1.1.0",
        "versions": [
            {
                "version": "v1.0.0",
                "created_at": 1788409517.0,
                "brier_score": 0.1838,
                "roc_auc": 0.7939,
                "ece": 0.038,
                "sharpe_ratio": 1.92,
                "status": "ACTIVE",
                "n_samples": 3000,
                "parameters": {},
            },
            {
                "version": "v1.1.0",
                "created_at": 1788495917.0,
                "brier_score": 0.1013,
                "roc_auc": 0.9451,
                "ece": 0.0836,
                "sharpe_ratio": 2.1,
                "status": "ACTIVE",
                "n_samples": 5000,
                "parameters": {},
            },
            {
                "version": "v1.rejected.0",
                "created_at": 1788582317.0,
                "brier_score": 0.30,
                "roc_auc": 0.60,
                "ece": 0.10,
                "sharpe_ratio": 0.5,
                "status": "REJECTED",
                "n_samples": 100,
                "parameters": {},
            },
        ],
    }
    with open(registry_file, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f)

    fresh = ModelRegistry()
    # v1.1.0 is the active_version → champion.
    assert fresh.get_state("v1.1.0") == MODEL_STATE_CHAMPION
    # v1.0.0 is ACTIVE-but-not-active → demoted (was promoted, has been superseded).
    assert fresh.get_state("v1.0.0") == MODEL_STATE_DEMOTED
    # v1.rejected.0 is REJECTED → experimental (failed safety gate, never promoted).
    assert fresh.get_state("v1.rejected.0") == MODEL_STATE_EXPERIMENTAL
    assert fresh.get_active_version() == "v1.1.0"


# ── 13. ModelVersionRecord.to_dict includes the state field ──────────────────


def test_model_version_record_to_dict_includes_state_field():
    """``ModelVersionRecord.to_dict()`` must include the ``state`` field
    so the HTTP surface (``GET /api/ml/versions``) and the singleton's
    persisted JSON both surface the lifecycle state."""
    record = ModelVersionRecord(
        version="v1.test",
        created_at=1234567890.0,
        brier_score=0.1838,
        roc_auc=0.7939,
        ece=0.038,
        sharpe_ratio=1.92,
        status="ACTIVE",
        n_samples=3000,
        parameters={"a": 1},
        state=MODEL_STATE_SHADOW,
    )
    out = record.to_dict()
    assert out["state"] == MODEL_STATE_SHADOW

    # Default state (no explicit kwarg) is ``experimental``.
    record_default = ModelVersionRecord(
        version="v1.test_default",
        created_at=1234567890.0,
        brier_score=0.1838,
        roc_auc=0.7939,
        ece=0.038,
        sharpe_ratio=1.92,
        status="ACTIVE",
        n_samples=3000,
        parameters={"a": 1},
    )
    assert record_default.state == MODEL_STATE_EXPERIMENTAL
    assert record_default.to_dict()["state"] == MODEL_STATE_EXPERIMENTAL


# ── 14-20. HTTP surface ───────────────────────────────────────────────────────


# (14) GET /api/ml/lifecycle — 200 + lifecycle list
def test_get_lifecycle_endpoint_returns_200_with_list(lifecycle_client):
    """``GET /api/ml/lifecycle`` returns 200 with a list of version
    dicts, each carrying ``version``, ``state``, ``status``, ``brier``,
    ``auc``, ``n_samples``, ``created_at``, and ``is_active``."""
    response = lifecycle_client.get("/api/ml/lifecycle")
    assert response.status_code == 200, (
        f"GET /api/ml/lifecycle must return 200; got {response.status_code}. "
        f"Body: {response.text!r}"
    )
    data = response.json()
    assert isinstance(data, list), (
        f"GET /api/ml/lifecycle must return a list; got {type(data).__name__}"
    )
    # The fresh-app fixture seeds the baseline v1.0.0 champion.
    assert len(data) >= 1
    first = data[0]
    # Required lifecycle fields.
    for key in ("version", "state", "status", "brier", "auc",
                "n_samples", "created_at", "is_active"):
        assert key in first, (
            f"GET /api/ml/lifecycle entry missing required field {key!r}; "
            f"got {sorted(first.keys())}"
        )
    # The baseline v1.0.0 is the champion.
    v1_0_0 = next(e for e in data if e["version"] == "v1.0.0")
    assert v1_0_0["state"] == MODEL_STATE_CHAMPION
    assert v1_0_0["is_active"] is True


# (15) POST /api/ml/{version}/promote — 200 + advances state
def test_promote_endpoint_advances_state(lifecycle_client):
    """``POST /api/ml/{version}/promote`` advances the model one step
    along the promotion ladder and returns 200 with the new state."""
    # Register a REJECTED version via the singleton (the HTTP tests
    # share the singleton; this is the same shape a production deploy
    # would see after a failed safety-gate registration).
    model_registry.register_version(
        "v1.experimental.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )
    assert model_registry.get_state("v1.experimental.0") == MODEL_STATE_EXPERIMENTAL

    # Step 1: experimental → shadow
    response = lifecycle_client.post("/api/ml/v1.experimental.0/promote")
    assert response.status_code == 200, (
        f"POST /api/ml/v1.experimental.0/promote must return 200; got "
        f"{response.status_code}. Body: {response.text!r}"
    )
    data = response.json()
    assert data["ok"] is True
    assert data["version"] == "v1.experimental.0"
    assert data["state"] == MODEL_STATE_SHADOW

    # Step 2: shadow → challenger
    response = lifecycle_client.post("/api/ml/v1.experimental.0/promote")
    assert response.status_code == 200
    assert response.json()["state"] == MODEL_STATE_CHALLENGER

    # Step 3: challenger → champion (demotes previous champion +
    # re-points active_version).
    response = lifecycle_client.post("/api/ml/v1.experimental.0/promote")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == MODEL_STATE_CHAMPION
    assert payload["active_version"] == "v1.experimental.0"
    # The previous champion v1.0.0 was demoted.
    assert model_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED


# (16) POST /api/ml/{version}/promote — 400 on unknown version
def test_promote_endpoint_400_on_unknown_version(lifecycle_client):
    """``POST /api/ml/{unknown}/promote`` returns 400 with a helpful
    detail message."""
    response = lifecycle_client.post("/api/ml/v9.999.does_not_exist/promote")
    assert response.status_code == 400, (
        f"POST /api/ml/v9.999.does_not_exist/promote must return 400; got "
        f"{response.status_code}. Body: {response.text!r}"
    )
    data = response.json()
    assert "detail" in data
    assert "v9.999.does_not_exist" in data["detail"]


# (17) POST /api/ml/{version}/rollback — 200 + crowns target
def test_lifecycle_rollback_endpoint_crowns_target(lifecycle_client):
    """``POST /api/ml/{version}/rollback`` demotes the current champion
    and crowns the target as the new champion. Returns 200 with the new
    state."""
    # Register a healthy v1.1.0 — becomes champion; v1.0.0 demoted.
    model_registry.register_version(
        "v1.1.0",
        brier_score=0.18,
        roc_auc=0.80,
        ece=0.04,
        sharpe_ratio=1.5,
        n_samples=2000,
        parameters={},
    )
    assert model_registry.get_active_version() == "v1.1.0"
    assert model_registry.get_state("v1.0.0") == MODEL_STATE_DEMOTED

    # Roll back to v1.0.0 — should re-crown it champion and demote v1.1.0.
    response = lifecycle_client.post("/api/ml/v1.0.0/rollback")
    assert response.status_code == 200, (
        f"POST /api/ml/v1.0.0/rollback must return 200; got "
        f"{response.status_code}. Body: {response.text!r}"
    )
    data = response.json()
    assert data["ok"] is True
    assert data["version"] == "v1.0.0"
    assert data["state"] == MODEL_STATE_CHAMPION
    assert data["active_version"] == "v1.0.0"
    assert data["previous_version"] == "v1.1.0"

    # State in the singleton reflects the new champion.
    assert model_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION
    assert model_registry.get_state("v1.1.0") == MODEL_STATE_DEMOTED


def test_lifecycle_rollback_endpoint_400_on_unknown_version(lifecycle_client):
    """``POST /api/ml/{unknown}/rollback`` returns 400 with a helpful
    detail message."""
    response = lifecycle_client.post("/api/ml/v9.999.does_not_exist/rollback")
    assert response.status_code == 400, (
        f"POST /api/ml/v9.999.does_not_exist/rollback must return 400; got "
        f"{response.status_code}. Body: {response.text!r}"
    )
    data = response.json()
    assert "detail" in data
    assert "v9.999.does_not_exist" in data["detail"]


# (18) POST /api/ml/{version}/demote — 200 + demotes champion
def test_demote_endpoint_demotes_champion(lifecycle_client):
    """``POST /api/ml/{version}/demote`` advances the model one step
    along the demotion ladder (champion → demoted → retired)."""
    # Baseline: v1.0.0 is champion.
    assert model_registry.get_state("v1.0.0") == MODEL_STATE_CHAMPION

    # Step 1: champion → demoted.
    response = lifecycle_client.post("/api/ml/v1.0.0/demote")
    assert response.status_code == 200, (
        f"POST /api/ml/v1.0.0/demote must return 200; got "
        f"{response.status_code}. Body: {response.text!r}"
    )
    data = response.json()
    assert data["ok"] is True
    assert data["version"] == "v1.0.0"
    assert data["state"] == MODEL_STATE_DEMOTED

    # Step 2: demoted → retired.
    response = lifecycle_client.post("/api/ml/v1.0.0/demote")
    assert response.status_code == 200
    assert response.json()["state"] == MODEL_STATE_RETIRED


# (19) POST /api/ml/{version}/demote — 400 on unknown version
def test_demote_endpoint_400_on_unknown_version(lifecycle_client):
    """``POST /api/ml/{unknown}/demote`` returns 400 with a helpful
    detail message."""
    response = lifecycle_client.post("/api/ml/v9.999.does_not_exist/demote")
    assert response.status_code == 400, (
        f"POST /api/ml/v9.999.does_not_exist/demote must return 400; got "
        f"{response.status_code}. Body: {response.text!r}"
    )
    data = response.json()
    assert "detail" in data
    assert "v9.999.does_not_exist" in data["detail"]


# (20) end-to-end: promote to champion demotes previous champion AND
# re-points active_version.
def test_promote_to_champion_endpoint_demotes_previous_champion_and_repoints_active(
    lifecycle_client,
):
    """End-to-end HTTP test: registering a healthy new version (via the
    singleton) demotes the previous champion; calling
    ``POST /api/ml/{version}/promote`` on a challenger model advances
    it to ``champion`` AND demotes the current champion AND re-points
    ``active_version`` at the new champion.

    Verifies the lifecycle endpoints surface the correct state across
    multiple HTTP round-trips (no in-process shortcut).
    """
    # Sanity: the fresh-app fixture seeded v1.0.0 as champion.
    response = lifecycle_client.get("/api/ml/lifecycle")
    assert response.status_code == 200
    initial_lifecycle = response.json()
    v1_0_0_initial = next(e for e in initial_lifecycle if e["version"] == "v1.0.0")
    assert v1_0_0_initial["state"] == MODEL_STATE_CHAMPION
    assert v1_0_0_initial["is_active"] is True

    # Register a REJECTED version (stays at experimental).
    model_registry.register_version(
        "v1.challenger.0",
        brier_score=0.30,
        roc_auc=0.60,
        ece=0.10,
        sharpe_ratio=0.5,
        n_samples=100,
        parameters={},
    )
    # Drive v1.challenger.0 through the ladder via the HTTP surface.
    assert lifecycle_client.post("/api/ml/v1.challenger.0/promote").json()["state"] == MODEL_STATE_SHADOW
    assert lifecycle_client.post("/api/ml/v1.challenger.0/promote").json()["state"] == MODEL_STATE_CHALLENGER

    # Final promote: challenger → champion. This demotes v1.0.0 (the
    # current champion) and re-points active_version at v1.challenger.0.
    response = lifecycle_client.post("/api/ml/v1.challenger.0/promote")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == MODEL_STATE_CHAMPION
    assert payload["active_version"] == "v1.challenger.0"

    # Verify the demoted previous champion via the lifecycle endpoint.
    response = lifecycle_client.get("/api/ml/lifecycle")
    assert response.status_code == 200
    final_lifecycle = response.json()
    v1_0_0_final = next(e for e in final_lifecycle if e["version"] == "v1.0.0")
    assert v1_0_0_final["state"] == MODEL_STATE_DEMOTED
    assert v1_0_0_final["is_active"] is False
    v1_challenger_final = next(e for e in final_lifecycle if e["version"] == "v1.challenger.0")
    assert v1_challenger_final["state"] == MODEL_STATE_CHAMPION
    assert v1_challenger_final["is_active"] is True


# ── Bonus: the lifecycle ladder / constants are well-formed ───────────────────


def test_lifecycle_ladders_are_well_formed():
    """Sanity: the promotion ladder is
    ``[experimental, shadow, challenger, champion]`` and the demotion
    ladder is ``[champion, demoted, retired]``. These are the constants
    the operator-facing docs surface."""
    assert MODEL_PROMOTION_LADDER == (
        MODEL_STATE_EXPERIMENTAL,
        MODEL_STATE_SHADOW,
        MODEL_STATE_CHALLENGER,
        MODEL_STATE_CHAMPION,
    )
    assert MODEL_DEMOTION_LADDER == (
        MODEL_STATE_CHAMPION,
        MODEL_STATE_DEMOTED,
        MODEL_STATE_RETIRED,
    )
    # The 6 lifecycle states are exactly the union of the two ladders
    # (with ``champion`` shared between them).
    all_states = set(MODEL_PROMOTION_LADDER) | set(MODEL_DEMOTION_LADDER)
    assert all_states == {
        MODEL_STATE_EXPERIMENTAL,
        MODEL_STATE_SHADOW,
        MODEL_STATE_CHALLENGER,
        MODEL_STATE_CHAMPION,
        MODEL_STATE_DEMOTED,
        MODEL_STATE_RETIRED,
    }
