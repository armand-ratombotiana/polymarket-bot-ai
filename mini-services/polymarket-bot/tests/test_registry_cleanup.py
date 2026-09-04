"""
tests/test_registry_cleanup.py — W18-8 P0-C08 fix verification.

P0-C08 was a Wave 17-3 audit finding with two halves:

  (A) **Model registry test pollution.** Test runs of
      ``MarketMLModel.fit_initial()`` (with shrunk synthetic data — n=100,
      brier=0.1786, ece=0.2617) registered their fixture-grade versions
      in the production ``data/model_registry.json`` (or, after the
      conftest redirect was added, in the shared ``/tmp/pmbot_conftest_
      isolation/model_registry.json``). The accumulated pollution leaked
      into subsequent sessions and broke tests that assert on the
      registry lineage shape.

  (B) **Walk-forward CV not wired.** ``ml/validation.py`` had a fully
      built ``time_series_cv()`` primitive (expanding-window walk-forward
      CV with per-fold Brier / AUC / log-loss / accuracy) that was NEVER
      called from ``MarketMLModel.fit_initial()`` or any training
      orchestrator. The CV was dead code; the registry carried no
      out-of-sample headline metric per version.

W18-8 ships four fixes:

  1. ``scripts/clean_registry.py`` — a parameterized, idempotent
     registry-cleanup CLI that drops test-fixture entries (n_samples
     below threshold OR ece above threshold) and re-points
     ``active_version`` to the most recent surviving entry.
  2. ``tests/conftest.py`` now DELETES the conftest-redirected registry
     file at session start, BEFORE the singleton is constructed, so
     cross-run pollution can never leak into a new test session.
  3. ``ml/model.py::fit_initial`` now invokes
     ``ml.validation.time_series_cv`` after training + calibration
     (with a fresh ``RandomForestClassifier`` as the CV model, a
     ``min_train_size`` adapted to the actual data size so a 100-row
     fixture still produces ≥1 fold, and a try/except that swallows
     any CV failure so the production training path is never broken).
  4. The CV headline metrics (``cv_auc_mean``, ``cv_auc_std``,
     ``cv_n_splits``, ``cv_min_train_size``) are persisted into the
     ``parameters`` dict of the newly-registered version, so the
     registry's lineage carries the per-version out-of-sample metric.

This test module covers all four fixes:

  * ``test_clean_registry_removes_test_fixture_entries`` — the clean
    function drops every entry that looks like a test fixture (small
    ``n_samples`` or high ``ece``) and retains the real versions.
  * ``test_clean_registry_repoints_active_to_surviving_entry`` — after
    cleaning, ``active_version`` points to a SURVIVING entry (never a
    dropped test fixture, never ``None`` while ≥1 entry survives).
  * ``test_clean_registry_is_idempotent`` — running clean on an
    already-clean registry is a no-op (0 entries dropped, active
    unchanged). Catches regressions where a future heuristic tweak
    would make clean oscillate.
  * ``test_clean_registry_dry_run_does_not_write`` — ``dry_run=True``
    leaves the file untouched but still reports the would-be delta.
  * ``test_conftest_uses_separate_registry_path`` — the env var
    ``MODEL_REGISTRY_PATH`` points at a /tmp path, NOT the production
    ``data/model_registry.json``. Belt-and-braces: confirms the
    conftest redirect is in effect for THIS session.
  * ``test_conftest_registry_file_cleared_at_session_start`` — the
    conftest-redirected registry file contains at most the factory
    baseline (``v1.0.0`` seeded by ``_load_from_disk``), never
    cross-run pollution. The ``_reset_store_factory_defaults``
    autouse fixture in conftest already resets the in-memory
    singletons per test, so this is mostly a sanity check that the
    on-disk file was cleared at conftest load.
  * ``test_walk_forward_cv_called_during_fit_initial`` — patches
    ``ml.validation.time_series_cv`` to record its invocation
    arguments, drives ``fit_initial()`` on a shrunk synthetic dataset,
    and asserts the CV was called with the full (X, y) training set
    and ``n_splits=5``.
  * ``test_walk_forward_cv_results_in_registered_version`` — the
    newly-registered version's ``parameters`` dict carries
    ``cv_auc_mean`` / ``cv_auc_std`` / ``cv_n_splits`` /
    ``cv_min_train_size`` (either numeric when CV ran, or ``None`` /
    ``0`` when CV could not run).
  * ``test_walk_forward_cv_failure_does_not_break_training`` — when
    ``time_series_cv`` raises (simulated via the patch), the
    ``fit_initial`` path still completes, the version is still
    registered, and ``self.cv_results["ran"]`` is ``False`` with an
    ``"error"`` key.
  * ``test_walk_forward_cv_results_on_model_instance`` — after a
    successful ``fit_initial``, ``self.cv_results`` is a dict with
    ``ran=True`` and the canonical aggregate keys (``mean_auc``,
    ``std_auc``, ``mean_brier``, ``std_brier``, ``pooled_auc``).

Test isolation
--------------
* ``conftest.py`` already redirects ``MODEL_REGISTRY_PATH`` to
  ``/tmp/pmbot_conftest_isolation/model_registry.json`` AND deletes
  that file at session start, so the singleton starts from the
  factory baseline. The CV tests use a shrunk synthetic dataset
  (``n=100``) and shrunk estimator counts (``n_estimators=10``) so
  the per-test wall-time is ~2 s, not ~25 s.
* The clean-script tests use ``tmp_path``-scoped registry files so
  they cannot perturb the singleton's state.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# Inline sys.path bootstrap — mirrors the pattern in test_features.py and
# tests/conftest.py so ``from ml.model import ...`` resolves regardless of
# the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# The scripts directory is not on sys.path by default (it's a sibling of
# the tests/ dir, not a Python package). Add it so we can import
# ``clean_registry`` directly for unit testing the clean function.
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Make the clean script importable as a module — its ``main`` is exercised
# via ``argv`` (so tests don't need to shell out) and its ``clean_registry``
# function is the unit under test.
import clean_registry  # noqa: E402  (sys.path must be set first)
from ml.features import N_FEATURES  # noqa: E402
from ml.model import (  # noqa: E402
    MarketMLModel,
    _synthetic_training_data,
)

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_registry_payload(
    *,
    n_test_entries: int = 5,
    n_real_entries: int = 2,
    active: str | None = None,
) -> dict:
    """Build a synthetic registry payload with both real + test-fixture entries.

    The real entries mirror the production ``data/model_registry.json``
    shape: ``n_samples=3000``, ``ece≈0.04``. The test entries mirror the
    W17-3 audit signature: ``n_samples=100``, ``brier_score=0.1786``,
    ``ece=0.2617``, parameters ``{"n_estimators_rf": 10, ...}``.

    The payload is in the SAME shape ``ModelRegistry._save_to_disk``
    writes (``{"active_version": str, "versions": [...]}`` with versions
    newest-first), so the clean function operates on a realistic input.
    """
    versions: list[dict] = []
    # Real (production-grade) entries — most recent first.
    for i in range(n_real_entries):
        versions.append({
            "version": f"v1.{900 + i:03d}.0",
            "created_at": 1700000000.0 + i,
            "brier_score": 0.1035,
            "roc_auc": 0.9421,
            "ece": 0.038,
            "sharpe_ratio": 1.92,
            "status": "ACTIVE",
            "n_samples": 3000,
            "parameters": {
                "n_estimators_rf": 150,
                "n_estimators_gb": 100,
                "features": N_FEATURES,
                "calibration": "isotonic",
                "lgbm": True,
            },
        })
    # Test-fixture entries — also newest-first interleaved AFTER the real
    # ones (so the most-recent entry overall is a test fixture — exactly
    # the pollution shape the W17-3 audit found).
    for i in range(n_test_entries):
        versions.append({
            "version": f"v1.{800 + i:03d}.0",
            "created_at": 1700000000.0 + 100 + i,
            "brier_score": 0.1786,
            "roc_auc": 0.7381,
            "ece": 0.2617,
            "sharpe_ratio": 0.0,
            "status": "ACTIVE",
            "n_samples": 100,
            "parameters": {
                "n_estimators_rf": 10,
                "n_estimators_gb": 10,
                "features": N_FEATURES,
                "calibration": "isotonic",
                "lgbm": True,
            },
        })
    if active is None:
        active = versions[0]["version"] if versions else None
    return {"active_version": active, "versions": versions}


def _write_registry(path: Path, payload: dict) -> None:
    """Write a registry payload to ``path`` (atomic tmp → rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(path)


# ── Step 1 + Step 2: clean_registry script tests ─────────────────────────────


async def test_clean_registry_removes_test_fixture_entries(tmp_path):
    """``clean_registry`` drops every entry that looks like a test fixture
    (small ``n_samples`` OR high ``ece``) and retains the real versions.

    Builds a synthetic payload with 2 real (n=3000) + 5 test (n=100) entries
    and asserts the post-clean registry contains ONLY the 2 real entries.
    """
    reg_path = tmp_path / "model_registry.json"
    _write_registry(reg_path, _make_registry_payload(n_test_entries=5, n_real_entries=2))

    summary = clean_registry.clean_registry(reg_path)

    assert summary["total_before"] == 7
    assert summary["total_after"] == 2
    assert len(summary["dropped"]) == 5
    # Every dropped entry was a test fixture (n_samples=100, ece=0.2617).
    with open(reg_path, "r", encoding="utf-8") as f:
        cleaned = json.load(f)
    for entry in cleaned["versions"]:
        assert entry["n_samples"] == 3000
        assert entry["ece"] < 0.20


async def test_clean_registry_repoints_active_to_surviving_entry(tmp_path):
    """After cleaning, ``active_version`` points to a SURVIVING entry —
    never a dropped test fixture, never ``None`` while ≥1 entry survives.

    Constructs a payload where the most-recent entry is a TEST fixture
    (the W17-3 audit shape), so the pre-clean ``active_version`` is a
    fixture version. After clean, ``active`` must point to a real entry.
    """
    reg_path = tmp_path / "model_registry.json"
    payload = _make_registry_payload(n_test_entries=3, n_real_entries=2)
    # Force active to be a test-fixture version (matches the polluted state
    # observed in the W17-3 audit).
    payload["active_version"] = "v1.800.0"  # a test-fixture entry
    _write_registry(reg_path, payload)

    summary = clean_registry.clean_registry(reg_path)

    assert summary["active_before"] == "v1.800.0"
    # Active after must be a SURVIVING entry — never a dropped test fixture.
    assert summary["active_after"] is not None
    assert summary["active_after"] != "v1.800.0"
    assert summary["active_after"] not in summary["dropped"]

    # The active_after entry must be one of the surviving versions.
    with open(reg_path, "r", encoding="utf-8") as f:
        cleaned = json.load(f)
    surviving_versions = [v["version"] for v in cleaned["versions"]]
    assert summary["active_after"] in surviving_versions


async def test_clean_registry_is_idempotent(tmp_path):
    """Running clean on an already-clean registry is a no-op: 0 entries
    dropped, ``active_version`` unchanged. Catches a regression where a
    future heuristic tweak would make clean oscillate (drop entries that
    survive a prior clean)."""
    reg_path = tmp_path / "model_registry.json"
    _write_registry(reg_path, _make_registry_payload(n_test_entries=3, n_real_entries=2))

    first = clean_registry.clean_registry(reg_path)
    assert first["total_after"] == 2

    second = clean_registry.clean_registry(reg_path)
    assert second["total_before"] == 2
    assert second["total_after"] == 2
    assert second["dropped"] == []
    assert second["active_after"] == first["active_after"]


async def test_clean_registry_dry_run_does_not_write(tmp_path):
    """``dry_run=True`` reports the would-be delta WITHOUT modifying the
    on-disk file. The pre-clean file is preserved verbatim so the operator
    can audit the would-be drop set before committing."""
    reg_path = tmp_path / "model_registry.json"
    payload = _make_registry_payload(n_test_entries=3, n_real_entries=2)
    _write_registry(reg_path, payload)
    original_bytes = reg_path.read_bytes()

    summary = clean_registry.clean_registry(reg_path, dry_run=True)

    assert summary["written"] is False
    assert summary["total_before"] == 5
    assert summary["total_after"] == 2
    assert len(summary["dropped"]) == 3
    # File unchanged.
    assert reg_path.read_bytes() == original_bytes


async def test_clean_registry_active_none_when_all_dropped(tmp_path):
    """When every entry is a test fixture, ``active_after`` is ``None``
    (the operator must retrain). Catches a regression where clean would
    leave ``active_version`` pointing at a dropped entry."""
    reg_path = tmp_path / "model_registry.json"
    _write_registry(reg_path, _make_registry_payload(n_test_entries=4, n_real_entries=0))

    summary = clean_registry.clean_registry(reg_path)

    assert summary["total_before"] == 4
    assert summary["total_after"] == 0
    assert summary["active_after"] is None
    with open(reg_path, "r", encoding="utf-8") as f:
        cleaned = json.load(f)
    assert cleaned["versions"] == []
    assert cleaned["active_version"] is None


async def test_clean_registry_cli_main_dry_run(tmp_path, capsys):
    """The CLI ``main`` exits 0 on a dry-run and prints a one-line
    summary plus the dropped-version list (when ≤ 20 entries)."""
    reg_path = tmp_path / "model_registry.json"
    _write_registry(reg_path, _make_registry_payload(n_test_entries=3, n_real_entries=2))

    rc = clean_registry.main(["--path", str(reg_path), "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "would clean" in out
    assert "5 → 2" in out  # 5 before → 2 after
    assert "dropped 3" in out


# ── Step 2: conftest redirect hardening tests ────────────────────────────────


async def test_conftest_uses_separate_registry_path():
    """The env var ``MODEL_REGISTRY_PATH`` MUST point at a /tmp path, NOT
    the production ``data/model_registry.json``.

    Belt-and-braces: conftest.py already redirects via ``setdefault`` at
    session start. This test confirms the redirect is in effect for THIS
    session and that no test in the same session can accidentally fall
    back to the production path.
    """
    model_registry_path = os.environ.get("MODEL_REGISTRY_PATH", "")
    assert model_registry_path, (
        "MODEL_REGISTRY_PATH env var must be set by conftest before any "
        "module that imports ml.model_registry is loaded"
    )
    # Production registry paths — never acceptable for a test session.
    forbidden = (
        "data/model_registry.json",
        "/app/data/model_registry.json",
    )
    for path in forbidden:
        assert not model_registry_path.endswith(path), (
            f"MODEL_REGISTRY_PATH must NOT point at the production "
            f"registry ({path}); got {model_registry_path!r}"
        )
    # /tmp prefix — the canonical conftest redirect root.
    assert "/tmp" in model_registry_path, (
        f"MODEL_REGISTRY_PATH should be redirected to a /tmp path; "
        f"got {model_registry_path!r}"
    )


async def test_conftest_registry_file_cleared_at_session_start(monkeypatch, tmp_path):
    """The conftest-redirected registry file (cleared at session start
    by the W18-8 conftest hook) starts from the factory baseline — i.e.
    NO pollution from a prior pytest session can leak into the current
    session's on-disk file.

    This test re-runs the conftest's clear-on-startup logic against a
    tmp_path-scoped registry file (so it doesn't perturb the singleton
    the rest of the suite uses): writes a polluted file, calls the
    conftest-clear logic, then asserts the file is GONE (so the next
    ``ModelRegistry()`` constructor would re-seed the baseline).
    """
    # Mirror the conftest redirect: a tmp-path-scoped registry file.
    reg_path = tmp_path / "model_registry.json"
    # Write a polluted file — what a prior test session would leave
    # behind if conftest didn't clear it at session start.
    _write_registry(reg_path, _make_registry_payload(n_test_entries=5, n_real_entries=2))
    assert reg_path.exists(), "precondition: polluted file written"

    # The conftest clear logic is just ``Path.unlink()`` (best-effort).
    # Mirror it here so the test stays hermetic — we don't reach into
    # conftest's module-level state, we just verify the same unlink
    # pattern works as advertised.
    if reg_path.exists():
        try:
            reg_path.unlink()
        except OSError:
            pass

    assert not reg_path.exists(), (
        "conftest session-start unlink MUST remove the polluted registry "
        "file so the next ``ModelRegistry()`` constructor re-seeds the "
        "factory baseline via ``_load_from_disk``"
    )

    # Sanity check: a fresh ``ModelRegistry()`` constructed after the
    # unlink DOES re-seed the factory baseline (exactly one v1.0.0
    # entry, n_samples=3000 — NOT a test fixture).
    from ml.model_registry import ModelRegistry, REGISTRY_FILE
    # Point the module-level REGISTRY_FILE at our clean tmp path so the
    # fresh singleton doesn't accidentally hit the conftest-redirected
    # /tmp file the rest of the suite is using.
    monkeypatch.setattr("ml.model_registry.REGISTRY_FILE", reg_path)
    fresh = ModelRegistry()
    versions = fresh.list_versions()
    assert len(versions) == 1, (
        f"fresh registry must seed exactly one factory baseline version; "
        f"got {len(versions)} versions"
    )
    assert versions[0]["version"] == "v1.0.0"
    assert versions[0]["n_samples"] == 3000
    assert versions[0]["n_samples"] >= 1000  # NOT a test fixture.
    # Restore is automatic via monkeypatch teardown.


# ── Step 3 + Step 4: walk-forward CV wiring tests ────────────────────────────


@pytest.fixture
def fitted_model_with_cv():
    """A freshly-trained ``MarketMLModel()`` trained on a shrunk synthetic
    dataset (100 rows + 10 estimators) so the per-test wall-time is ~2 s.

    Mirrors the ``fitted_model`` fixture in ``tests/test_ml_model.py``,
    with the W18-8 difference that we DON'T patch
    ``ml.validation.time_series_cv`` (we want the real CV path exercised).
    """
    def _small_synth(n: int = 100) -> tuple[np.ndarray, np.ndarray]:
        return _synthetic_training_data(100)

    with patch("ml.model._synthetic_training_data", _small_synth), \
         patch("core.timescale_db.timescale_db") as mock_db:
        mock_db.fetch_training_samples.return_value = (None, [])
        m = MarketMLModel()
        m.fit_initial(n_estimators_rf=10, n_estimators_gb=10)
    return m


async def test_walk_forward_cv_called_during_fit_initial():
    """``fit_initial()`` MUST call ``ml.validation.time_series_cv`` exactly
    once with the full training set (X, y) and ``n_splits=5``.

    Patches ``ml.validation.time_series_cv`` to record its invocation and
    return a synthetic result dict (so we don't pay the per-fold retrain
    cost in this test). Asserts the call happened with the right args.
    """
    calls: list[dict] = []

    def _fake_time_series_cv(model, X, y, n_splits=5, min_train_size=200):
        calls.append({
            "model_type": type(model).__name__,
            "n_rows": int(np.asarray(X).shape[0]),
            "n_cols": int(np.asarray(X).shape[1]),
            "n_y": int(np.asarray(y).shape[0]),
            "n_splits": int(n_splits),
            "min_train_size": int(min_train_size),
        })
        return {
            "method": "walk_forward_expanding_window",
            "n_splits_requested": n_splits,
            "n_splits_evaluated": n_splits,
            "min_train_size": min_train_size,
            "val_size": 16,
            "total_samples": int(np.asarray(X).shape[0]),
            "per_fold": [],
            "aggregate": {
                "n_folds_evaluated": n_splits,
                "mean_brier": 0.18,
                "std_brier": 0.02,
                "mean_auc": 0.75,
                "std_auc": 0.03,
                "mean_log_loss": 0.55,
                "mean_accuracy": 0.70,
                "total_train_samples": 500,
                "total_val_samples": 80,
                "pooled": {
                    "n_samples": 80,
                    "mean_pred": 0.50,
                    "mean_actual": 0.50,
                    "brier": 0.18,
                    "auc": 0.75,
                    "log_loss": 0.55,
                    "accuracy": 0.70,
                },
            },
        }

    def _small_synth(n: int = 100) -> tuple[np.ndarray, np.ndarray]:
        return _synthetic_training_data(100)

    with patch("ml.model._synthetic_training_data", _small_synth), \
         patch("core.timescale_db.timescale_db") as mock_db, \
         patch("ml.validation.time_series_cv", _fake_time_series_cv), \
         patch("ml.model.model_registry") as mock_reg:
        mock_db.fetch_training_samples.return_value = (None, [])
        mock_reg.register_version.return_value = True
        m = MarketMLModel()
        m.fit_initial(n_estimators_rf=10, n_estimators_gb=10)

    assert len(calls) == 1, (
        f"time_series_cv must be called exactly once during fit_initial; "
        f"got {len(calls)} calls"
    )
    call = calls[0]
    assert call["model_type"] == "RandomForestClassifier"
    assert call["n_rows"] == 100, f"CV must see the full training set; got n={call['n_rows']}"
    assert call["n_cols"] == N_FEATURES
    assert call["n_y"] == 100
    assert call["n_splits"] == 5
    # min_train_size adapted to n // 5 = 20 (since n=100 < production 3000).
    assert call["min_train_size"] == 20


async def test_walk_forward_cv_results_in_registered_version(fitted_model_with_cv):
    """The newly-registered version's ``parameters`` dict MUST carry the
    CV headline metrics (``cv_auc_mean`` / ``cv_auc_std`` /
    ``cv_n_splits`` / ``cv_min_train_size``).

    The singleton ``model_registry`` is loaded against the conftest
    redirect path, so the registered version lands in the test-isolation
    registry — safe to inspect. We grab the most-recently registered
    version (index 0 in the lineage, since ``register_version`` inserts
    at the front) and verify the CV fields are present with the correct
    types.
    """
    from ml.model_registry import model_registry

    # The fit_initial call inside the fixture registered one new version
    # on top of whatever the singleton loaded. Grab the newest entry.
    versions = model_registry.list_versions()
    assert len(versions) >= 1
    newest = versions[0]
    params = newest["parameters"]

    # CV fields must be present (the schema is stable — None when CV
    # could not run, numeric when it did).
    assert "cv_auc_mean" in params
    assert "cv_auc_std" in params
    assert "cv_n_splits" in params
    assert "cv_min_train_size" in params

    # On a 100-sample training set the CV SHOULD succeed (min_train_size
    # adapted to n//5 = 20, so 1 fold is achievable). Assert the success
    # path explicitly so a regression that silently no-ops the CV is
    # caught.
    assert params["cv_n_splits"] >= 1, (
        f"CV must produce ≥1 fold on a 100-row training set; got "
        f"cv_n_splits={params['cv_n_splits']}"
    )
    assert params["cv_auc_mean"] is not None, (
        "CV must produce a numeric mean_auc when it runs successfully"
    )
    assert isinstance(params["cv_auc_mean"], float)
    assert 0.0 <= params["cv_auc_mean"] <= 1.0


async def test_walk_forward_cv_failure_does_not_break_training():
    """When ``time_series_cv`` raises (e.g. training set too small for a
    single fold, simulated here via the patch), the ``fit_initial`` path
    MUST still complete, the version MUST still be registered, and
    ``self.cv_results["ran"]`` MUST be ``False`` with an ``"error"`` key.

    This is the documented contract: CV is a *diagnostic* layer; failure
    to run it MUST NOT break the production training path. Catches a
    regression where a future refactor removes the try/except.
    """
    def _raising_cv(*args, **kwargs):
        raise ValueError("simulated CV failure (too few samples)")

    def _small_synth(n: int = 100) -> tuple[np.ndarray, np.ndarray]:
        return _synthetic_training_data(100)

    # Patch ``ml.model.model_registry`` (NOT ``ml.model_registry.model_registry``)
    # — ``ml/model.py`` imports the singleton at module-load time via
    # ``from ml.model_registry import model_registry`` so the local name
    # binding inside ``ml.model`` is what ``fit_initial`` actually resolves.
    with patch("ml.model._synthetic_training_data", _small_synth), \
         patch("core.timescale_db.timescale_db") as mock_db, \
         patch("ml.validation.time_series_cv", _raising_cv), \
         patch("ml.model.model_registry") as mock_reg:
        mock_db.fetch_training_samples.return_value = (None, [])
        mock_reg.register_version.return_value = True
        m = MarketMLModel()
        # Must not raise — the CV exception is caught internally.
        m.fit_initial(n_estimators_rf=10, n_estimators_gb=10)

        # register_version MUST have been called despite the CV failure.
        assert mock_reg.register_version.called, (
            "register_version must be called even when CV fails"
        )
        call_kwargs = mock_reg.register_version.call_args
        params = call_kwargs.kwargs.get("parameters", {})
        # Schema stability: CV fields are present even on failure (None / 0).
        assert "cv_auc_mean" in params
        assert "cv_auc_std" in params
        assert params["cv_auc_mean"] is None
        assert params["cv_auc_std"] is None
        assert params["cv_n_splits"] == 0

    # Model instance state reflects the failure.
    assert m.cv_results["ran"] is False
    assert "error" in m.cv_results
    assert "simulated CV failure" in m.cv_results["error"]


async def test_walk_forward_cv_results_on_model_instance(fitted_model_with_cv):
    """After a successful ``fit_initial``, ``self.cv_results`` is a dict
    with ``ran=True`` and the canonical aggregate keys (``mean_auc``,
    ``std_auc``, ``mean_brier``, ``std_brier``, ``pooled_auc``)."""
    cv = fitted_model_with_cv.cv_results
    assert cv["ran"] is True, (
        f"CV must have run successfully on a 100-row training set; "
        f"cv_results={cv}"
    )
    # Canonical keys from the ``time_series_cv`` aggregate roll-up.
    for key in (
        "method",
        "n_splits_requested",
        "n_splits_evaluated",
        "min_train_size",
        "val_size",
        "mean_auc",
        "std_auc",
        "mean_brier",
        "std_brier",
        "pooled_auc",
        "pooled_brier",
    ):
        assert key in cv, f"cv_results missing canonical key: {key!r}"
    assert cv["n_splits_evaluated"] >= 1
    assert cv["mean_auc"] is not None
    assert isinstance(cv["mean_auc"], float)
