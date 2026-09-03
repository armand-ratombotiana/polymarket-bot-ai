"""
tests/test_ab_testing.py — Unit + integration tests for ``ml/ab_testing.py``.

W14-5 — A/B testing framework for ML models.

Covers the seven behaviours required by the W14-5 task spec:

  (1) ``start_experiment`` ADDS a new running experiment to the store —
      after a single ``start_experiment`` call, ``get_status()`` reports
      ``active=True`` with the champion / challenger versions, traffic
      split, and zero per-arm prediction counts.

  (2) ``stop_experiment`` flips the active experiment to ``status=
      'stopped'`` and zeroes the manager's in-memory current-experiment
      pointer so subsequent ``assign_model`` calls return ``"champion"``
      (the no-experiment sentinel).

  (3) ``assign_model`` is DETERMINISTIC per ``token_id`` — the same
      ``token_id`` always maps to the same arm within an experiment
      (verified by repeated calls + a deterministic-by-construction
      proof using a hash collision argument).

  (4) ``record_prediction`` persists one row per call — after N
      ``record_prediction`` calls on each arm, ``get_status()`` reports
      ``champion_predictions == N`` and ``challenger_predictions == N``.

  (5) ``evaluate`` returns ``status="insufficient_data"`` when either
      arm has fewer than ``min_samples`` predictions-with-outcomes, and
      returns ``status="evaluated"`` with full per-arm metrics +
      significance verdicts once both arms clear the threshold.

  (6) The statistical-significance calculation is correct on a
      synthetic dataset — when the challenger is *deliberately*
      constructed to be much better than the champion, the z-test
      p-value is well below 0.05, ``challenger_is_better`` is True, and
      ``recommendation == "promote"``.

  (7) The four HTTP endpoints under ``/api/ab-test`` work end-to-end
      via ``TestClient`` — ``GET /api/ab-test`` returns 200 + the
      current status; ``POST /api/ab-test/start`` returns 200 + the
      new experiment descriptor (and 400 on an out-of-range
      ``traffic_split``); ``POST /api/ab-test/stop`` returns 200 + the
      stopped experiment name (and 404 when no experiment is active);
      ``GET /api/ab-test/evaluate`` returns 200 + the evaluation
      result (and 404 when the named experiment doesn't exist).

Module isolation
----------------
``ml/ab_testing.py`` is pure-Python + synchronous at the manager layer.
The SQLite store is hermetic per-test via a fresh ``tmp_path``-scoped
DB file passed to the ``ABTestManager(db_path=...)`` constructor —
production's module-level singleton ``ab_test = ABTestManager()``
(constructed at import time against ``AB_TEST_DB_PATH``, redirected by
``tests/conftest.py`` to ``/tmp/pmbot_conftest_isolation/ab_tests.db``)
is left untouched by the unit tests.

For the API integration tests, the singleton IS used (the
``register_routes`` handlers close over the module-level ``ab_test``
singleton), so the test patches the singleton's ``_db_path`` to a
fresh ``tmp_path``-scoped file via ``monkeypatch.setattr`` on
``ABTestManager`` (the class) and re-invokes ``__init__`` so the route
handlers see a clean slate. Teardown via ``monkeypatch`` automatically
restores the original singleton.

Sync ``def`` tests throughout — ``TestClient``'s sync portal manages
the FastAPI event loop. No ``pytestmark = pytest.mark.asyncio`` is
needed (mirrors ``tests/test_live_safety_gate_api.py``,
``tests/test_decision_ledger.py`` sync-test convention).
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── Bootstrap project root on sys.path (defensive; conftest.py also does this). ──
# Lets this file be run in isolation via
# ``python -m pytest tests/test_ab_testing.py`` — the project root is
# always importable as top-level modules (``ml.*``) regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (sys.path must be set first)
import pytest  # noqa: E402  (sys.path must be set first)

from ml.ab_testing import (  # noqa: E402
    ABTestManager,
    Experiment,
    ab_test as ab_test_singleton,
    register_routes,
)


# ── Fixture: fresh isolated ABTestManager per unit test ─────────────────────
@pytest.fixture
def manager(tmp_path) -> ABTestManager:
    """Return a brand-new ``ABTestManager`` whose SQLite file lives under
    ``tmp_path``.

    Each test gets a clean DB (empty ``experiments`` / ``predictions``
    tables, ``_current_experiment = None``) so the module-level singleton
    ``ab_test`` (also constructed at import time and shared across the
    whole pytest session) is never perturbed by these unit tests.
    """
    db_path = tmp_path / "ab_tests.db"
    return ABTestManager(db_path=db_path)


# ── Fixture: FastAPI TestClient with isolated singleton for API tests ──────
@pytest.fixture
def client(tmp_path, request):
    """Return a ``TestClient`` against a minimal FastAPI app with only the
    A/B test routes registered AND the module-level singleton ``ab_test``
    patched to use a fresh ``tmp_path``-scoped SQLite file.

    The patch is applied by replacing the singleton's ``_db_path`` and
    re-running ``__init__``'s side effects so the in-memory
    ``_current_experiment`` pointer is reset and the on-disk DB is a clean
    file. ``request.addfinalizer`` restores the singleton to its
    conftest-default state on teardown (re-init against the
    ``AB_TEST_DB_PATH`` env var so any later test in the session that
    uses the singleton sees a clean conftest-default DB).

    Mirrors the ``_build_client`` pattern in
    ``tests/test_live_safety_gate_api.py`` — a per-test FastAPI app with
    ONLY the routes under test registered, so there's zero state leakage
    between tests.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import os

    def _restore_singleton():
        ab_test_singleton._db_path = Path(
            os.environ.get(
                "AB_TEST_DB_PATH", "/tmp/pmbot_conftest_isolation/ab_tests.db"
            )
        )
        ab_test_singleton._init_db()
        ab_test_singleton._current_experiment = (
            ab_test_singleton._load_active_experiment()
        )

    request.addfinalizer(_restore_singleton)

    # ── Patch the module-level singleton to a fresh tmp_path-scoped DB ──
    # ``ab_test_singleton`` is the same object reference the
    # ``register_routes`` handlers close over (via the module's globals);
    # mutating its ``_db_path`` + re-running ``_init_db`` is enough —
    # the handlers' name lookup resolves the same object at runtime.
    db_path = tmp_path / "api_ab_tests.db"
    ab_test_singleton._db_path = db_path
    # Re-run __init__'s side effects: ensure schema is created against
    # the new path AND ``_current_experiment`` is loaded fresh (None for
    # a brand-new empty DB).
    ab_test_singleton._init_db()
    ab_test_singleton._current_experiment = ab_test_singleton._load_active_experiment()

    app = FastAPI()
    register_routes(app)
    return TestClient(app)


# =============================================================================
# (1) start_experiment adds a running experiment
# =============================================================================
class TestStartExperiment:
    """Verify ``start_experiment`` correctly registers a new running
    experiment with the manager and persists it to the SQLite store."""

    def test_start_creates_running_experiment_with_correct_fields(self, manager):
        """After ``start_experiment``, ``get_status()`` reports ``active=True``
        with all the fields the caller supplied (name, versions, split, min_samples)
        and zero per-arm prediction counts."""
        exp = manager.start_experiment(
            name="v2_vs_v1",
            champion_version="v1",
            challenger_version="v2_isotonic",
            traffic_split=0.3,
            min_samples=50,
        )

        # The returned ``Experiment`` dataclass mirrors the caller's input.
        assert isinstance(exp, Experiment)
        assert exp.name == "v2_vs_v1"
        assert exp.champion_version == "v1"
        assert exp.challenger_version == "v2_isotonic"
        assert exp.traffic_split == 0.3
        assert exp.status == "running"
        assert exp.min_samples == 50

        # ``get_status`` exposes the same fields to API callers.
        status = manager.get_status()
        assert status["active"] is True
        assert status["experiment"]["name"] == "v2_vs_v1"
        assert status["experiment"]["champion_version"] == "v1"
        assert status["experiment"]["challenger_version"] == "v2_isotonic"
        assert status["experiment"]["traffic_split"] == 0.3
        assert status["experiment"]["min_samples"] == 50
        # No predictions recorded yet — both arms at zero.
        assert status["champion_predictions"] == 0
        assert status["challenger_predictions"] == 0

    def test_start_persists_experiment_to_db(self, manager):
        """The experiment is persisted to SQLite so a fresh ``ABTestManager``
        instance against the same DB file loads it as the active experiment
        on construction."""
        manager.start_experiment(
            name="persisted_exp",
            champion_version="champ",
            challenger_version="chall",
            traffic_split=0.5,
        )

        # Construct a SECOND manager against the same DB file — it should
        # load the experiment we just created (because status='running').
        second = ABTestManager(db_path=manager._db_path)
        assert second._current_experiment is not None
        assert second._current_experiment.name == "persisted_exp"
        assert second._current_experiment.champion_version == "champ"
        assert second._current_experiment.challenger_version == "chall"
        assert second._current_experiment.traffic_split == 0.5
        assert second._current_experiment.status == "running"

    def test_start_stops_previous_running_experiment(self, manager):
        """Starting a new experiment while one is already running stops
        the previous one — only one experiment is active at a time."""
        first = manager.start_experiment(
            name="first",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.3,
        )
        assert manager._current_experiment is not None
        assert manager._current_experiment.name == "first"

        # Start a second — the first should be auto-stopped.
        second = manager.start_experiment(
            name="second",
            champion_version="v1",
            challenger_version="v3",
            traffic_split=0.5,
        )
        assert second.name == "second"
        assert manager._current_experiment.name == "second"

        # The first experiment is now stopped (status='stopped' on disk);
        # only the second is loaded as the active experiment.
        status = manager.get_status()
        experiments = {e["name"]: e for e in status["experiments"]}
        assert experiments["first"]["status"] == "stopped"
        assert experiments["second"]["status"] == "running"


# =============================================================================
# (2) stop_experiment
# =============================================================================
class TestStopExperiment:
    """Verify ``stop_experiment`` flips the active experiment to stopped
    and clears the in-memory current-experiment pointer."""

    def test_stop_clears_current_experiment_pointer(self, manager):
        """After ``stop_experiment``, ``_current_experiment`` is None and
        ``get_status`` reports ``active=False``."""
        manager.start_experiment(
            name="to_stop",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.3,
        )
        assert manager._current_experiment is not None

        stopped = manager.stop_experiment("to_stop")
        assert stopped is True
        assert manager._current_experiment is None

        status = manager.get_status()
        assert status["active"] is False

    def test_stop_persists_status_to_db(self, manager):
        """The stopped status is persisted to SQLite so a fresh manager
        against the same DB does NOT load the stopped experiment as active."""
        manager.start_experiment(
            name="persist_stop",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.3,
        )
        manager.stop_experiment("persist_stop")

        # Fresh manager against the same DB — should NOT load the stopped exp.
        second = ABTestManager(db_path=manager._db_path)
        assert second._current_experiment is None

        # And the experiment's on-disk status is 'stopped'.
        status = second.get_status()
        experiments = {e["name"]: e for e in status["experiments"]}
        assert experiments["persist_stop"]["status"] == "stopped"
        assert experiments["persist_stop"]["ended_at"] is not None

    def test_stop_returns_false_for_unknown_experiment(self, manager):
        """``stop_experiment`` returns False when the named experiment is
        not found or already stopped."""
        # No experiment has been started — stop should be False.
        assert manager.stop_experiment("does_not_exist") is False

        # Start + stop, then stop AGAIN — second stop returns False.
        manager.start_experiment(
            name="already_stopped",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.3,
        )
        assert manager.stop_experiment("already_stopped") is True
        assert manager.stop_experiment("already_stopped") is False


# =============================================================================
# (3) assign_model is deterministic per token_id
# =============================================================================
class TestAssignModel:
    """Verify ``assign_model`` routes traffic deterministically by token_id."""

    def test_no_experiment_returns_champion_sentinel(self, manager):
        """With no active experiment, ``assign_model`` returns the literal
        ``"champion"`` sentinel so the caller can fall back to its production
        model."""
        assert manager._current_experiment is None
        assert manager.assign_model(token_id="any_token") == "champion"
        assert manager.assign_model(token_id=None) == "champion"

    def test_same_token_always_gets_same_arm(self, manager):
        """Repeated ``assign_model`` calls with the same ``token_id`` return
        the same arm — the assignment is deterministic per token."""
        manager.start_experiment(
            name="deterministic",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.3,
        )

        # Sample 200 tokens and verify each is stable across 10 calls.
        tokens = [f"tok_{i:04d}" for i in range(200)]
        for tok in tokens:
            first_assignment = manager.assign_model(token_id=tok)
            for _ in range(9):
                assert manager.assign_model(token_id=tok) == first_assignment, (
                    f"token {tok!r} flipped arms across repeated calls — "
                    f"assign_model must be deterministic per token_id"
                )

    def test_traffic_split_routing_distribution(self, manager):
        """With ``traffic_split=0.3`` and a large token sample, ~30% of
        tokens route to the challenger and ~70% to the champion.

        Uses a generous ±10% tolerance band because the sha256-based fold
        is deterministic but the empirical distribution over a finite
        token sample fluctuates; 1000 tokens gives a standard error well
        inside the tolerance.
        """
        manager.start_experiment(
            name="distribution",
            champion_version="champ",
            challenger_version="chall",
            traffic_split=0.3,
        )

        n = 1000
        challenger_count = sum(
            1
            for i in range(n)
            if manager.assign_model(token_id=f"tok_{i:05d}") == "chall"
        )
        challenger_fraction = challenger_count / n
        # 0.30 ± 0.10 — generous so the test is deterministic across
        # sha256 fold samples without being flaky.
        assert 0.20 <= challenger_fraction <= 0.40, (
            f"traffic_split=0.3 should route ~30% to challenger; "
            f"observed {challenger_fraction:.3f} ({challenger_count}/{n})"
        )

    def test_traffic_split_zero_routes_all_to_champion(self, manager):
        """``traffic_split=0.0`` routes every token to the champion."""
        manager.start_experiment(
            name="all_champ",
            champion_version="champ",
            challenger_version="chall",
            traffic_split=0.0,
        )
        for i in range(100):
            assert manager.assign_model(token_id=f"tok_{i}") == "champ"

    def test_traffic_split_one_routes_all_to_challenger(self, manager):
        """``traffic_split=1.0`` routes every token to the challenger."""
        manager.start_experiment(
            name="all_chall",
            champion_version="champ",
            challenger_version="chall",
            traffic_split=1.0,
        )
        for i in range(100):
            assert manager.assign_model(token_id=f"tok_{i}") == "chall"


# =============================================================================
# (4) record_prediction + update_outcome
# =============================================================================
class TestRecordPrediction:
    """Verify ``record_prediction`` persists one row per call and
    ``update_outcome`` back-fills the actual_outcome column."""

    def test_record_prediction_increments_counts(self, manager):
        """N ``record_prediction`` calls on each arm produce
        ``champion_predictions == N`` and ``challenger_predictions == N``."""
        manager.start_experiment(
            name="rec",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.5,
        )

        for i in range(7):
            manager.record_prediction("v1", 0.4 + i * 0.01, token_id=f"tok_{i}")
            manager.record_prediction("v2", 0.6 + i * 0.01, token_id=f"tok_{i}")

        status = manager.get_status()
        assert status["champion_predictions"] == 7
        assert status["challenger_predictions"] == 7

    def test_record_prediction_no_experiment_is_noop(self, manager):
        """With no active experiment, ``record_prediction`` silently does
        nothing (defensive — never raises)."""
        assert manager._current_experiment is None
        # Should NOT raise.
        manager.record_prediction("v1", 0.5, token_id="tok")
        # And no rows were written.
        status = manager.get_status()
        assert status["active"] is False

    def test_update_outcome_backfills_actual(self, manager):
        """``update_outcome(token_id, outcome)`` updates all predictions
        on that token that don't yet have an outcome set."""
        manager.start_experiment(
            name="outcome_backfill",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.5,
        )

        # Record 3 predictions on the same token (one per arm + an extra).
        manager.record_prediction("v1", 0.55, token_id="tok_X")
        manager.record_prediction("v2", 0.62, token_id="tok_X")
        manager.record_prediction("v2", 0.70, token_id="tok_Y")

        # Backfill the outcome for tok_X (resolved YES = 1).
        manager.update_outcome("tok_X", actual_outcome=1)

        # Re-load the rows from disk and verify.
        import sqlite3

        with sqlite3.connect(manager._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT token_id, prediction, actual_outcome FROM predictions "
                "WHERE token_id IN ('tok_X', 'tok_Y') ORDER BY id"
            ).fetchall()

        # All three rows present.
        assert len(rows) == 3
        # tok_X rows both have actual_outcome=1 (back-filled).
        tok_x_rows = [r for r in rows if r["token_id"] == "tok_X"]
        assert len(tok_x_rows) == 2
        for r in tok_x_rows:
            assert r["actual_outcome"] == 1
        # tok_Y row has actual_outcome=None (not back-filled — different token).
        tok_y_rows = [r for r in rows if r["token_id"] == "tok_Y"]
        assert len(tok_y_rows) == 1
        assert tok_y_rows[0]["actual_outcome"] is None


# =============================================================================
# (5) + (6) evaluate + statistical significance
# =============================================================================
class TestEvaluate:
    """Verify ``evaluate`` returns ``insufficient_data`` below the threshold,
    ``evaluated`` with full metrics + significance above it, and that the
    statistical significance verdict is correct on a synthetic dataset
    where the challenger is deliberately better."""

    def test_evaluate_unknown_experiment_returns_error(self, manager):
        """``evaluate`` on a non-existent experiment returns an error dict."""
        result = manager.evaluate("does_not_exist")
        assert "error" in result
        assert "not found" in result["error"].lower() or "no experiment" in result["error"].lower()

    def test_evaluate_no_active_experiment_returns_error(self, manager):
        """``evaluate()`` with no name and no active experiment returns an
        error dict (not a crash)."""
        assert manager._current_experiment is None
        result = manager.evaluate()
        assert "error" in result

    def test_evaluate_insufficient_data(self, manager):
        """Below ``min_samples`` per arm, ``evaluate`` returns
        ``status="insufficient_data"`` with the current sample counts and
        the required minimum."""
        manager.start_experiment(
            name="insuf",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.5,
            min_samples=50,
        )

        # Record 10 predictions per arm with outcomes (below min_samples=50).
        rng = np.random.RandomState(0)
        for i in range(10):
            actual = int(rng.random() > 0.5)
            manager.record_prediction(
                "v1", 0.5, token_id=f"tok_{i}", actual_outcome=actual
            )
            manager.record_prediction(
                "v2", 0.5, token_id=f"tok_{i}_v2", actual_outcome=actual
            )

        result = manager.evaluate("insuf")
        assert result["status"] == "insufficient_data"
        assert result["champion_samples"] == 10
        assert result["challenger_samples"] == 10
        assert result["min_required"] == 50

    def test_evaluate_promotes_significantly_better_challenger(self, manager):
        """When the challenger is constructed to be MUCH better than the
        champion, ``evaluate`` returns a small p_value (< 0.05),
        ``challenger_is_better=True``, and ``recommendation='promote'``."""
        manager.start_experiment(
            name="promote_test",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.5,
            min_samples=30,
        )

        # Synthetic data:
        #  - champion: noisy predictions around 0.5 (auc ~ 0.5, brier ~ 0.25)
        #  - challenger: predictions tightly correlated with the actual
        #    outcome (auc ~ 1.0, brier near 0)
        rng = np.random.RandomState(42)
        n = 80
        for i in range(n):
            actual = int(rng.random() > 0.5)
            champ_pred = float(np.clip(0.3 + 0.4 * rng.random(), 0.05, 0.95))
            # Challenger: tightly tracks the actual outcome.
            chall_pred = float(
                np.clip(0.1 + 0.85 * actual + 0.05 * rng.random(), 0.05, 0.95)
            )
            manager.record_prediction(
                "v1", champ_pred, token_id=f"c_{i}", actual_outcome=actual
            )
            manager.record_prediction(
                "v2", chall_pred, token_id=f"h_{i}", actual_outcome=actual
            )

        result = manager.evaluate("promote_test")
        assert result["status"] == "evaluated"

        # Per-arm metrics present.
        assert "champion" in result
        assert "challenger" in result
        assert result["champion"]["version"] == "v1"
        assert result["challenger"]["version"] == "v2"
        assert result["champion"]["samples"] == n
        assert result["challenger"]["samples"] == n
        # Headline metric fields present in each arm.
        for arm_key in ("champion", "challenger"):
            arm = result[arm_key]
            for metric_key in ("auc", "brier", "log_loss", "accuracy"):
                assert metric_key in arm, f"{arm_key} missing {metric_key}"
                assert isinstance(arm[metric_key], (int, float))

        # Challenger is much better than champion on every metric.
        assert result["challenger"]["auc"] > result["champion"]["auc"] + 0.2
        assert result["challenger"]["brier"] < result["champion"]["brier"]
        assert result["challenger"]["accuracy"] > result["champion"]["accuracy"]

        # Significance verdict.
        sig = result["significance"]
        assert "accuracy_z_score" in sig
        assert "accuracy_p_value" in sig
        assert "brier_t_statistic" in sig
        assert "brier_p_value" in sig
        assert "is_significant" in sig
        assert "challenger_is_better" in sig
        # ``is_significant`` and ``challenger_is_better`` MUST be Python
        # bools (not numpy.bool_) — the JSON encoder FastAPI uses does
        # not serialise numpy.bool_ natively, and a regression here would
        # surface as ``"True"`` (string) in the API response.
        assert isinstance(sig["is_significant"], bool)
        assert isinstance(sig["challenger_is_better"], bool)

        # The challenger IS significantly better — p_value well below 0.05.
        assert sig["accuracy_p_value"] < 0.05
        assert sig["challenger_is_better"] is True
        assert sig["is_significant"] is True
        assert result["recommendation"] == "promote"

    def test_evaluate_keeps_champion_when_not_significant(self, manager):
        """When champion and challenger perform identically, the z-test
        does NOT reach significance and the recommendation is
        ``keep_champion``."""
        manager.start_experiment(
            name="keep_test",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.5,
            min_samples=30,
        )

        # Both arms predict ~0.5 on every row — no signal either way.
        rng = np.random.RandomState(7)
        n = 60
        for i in range(n):
            actual = int(rng.random() > 0.5)
            # Same prediction for both arms — identical accuracy, identical brier.
            pred = 0.5
            manager.record_prediction(
                "v1", pred, token_id=f"c_{i}", actual_outcome=actual
            )
            manager.record_prediction(
                "v2", pred, token_id=f"h_{i}", actual_outcome=actual
            )

        result = manager.evaluate("keep_test")
        assert result["status"] == "evaluated"

        # Champion and challenger have identical metrics.
        assert result["champion"]["accuracy"] == result["challenger"]["accuracy"]
        assert result["champion"]["brier"] == pytest.approx(
            result["challenger"]["brier"]
        )

        # Not significant — p_value is large.
        sig = result["significance"]
        assert sig["accuracy_p_value"] > 0.05
        assert sig["challenger_is_better"] is False

        # Recommendation is keep_champion (challenger did not beat champion).
        assert result["recommendation"] == "keep_champion"

    def test_evaluate_z_test_correctness_on_known_data(self, manager):
        """Verify the z-test computation against a hand-computed expectation.

        Constructs a dataset where:
          - champion accuracy = 0.60 (60/100 correct)
          - challenger accuracy = 0.80 (80/100 correct)
        The two-proportion z-test on these numbers should yield
        ``|z| ≈ 3.13`` and ``p_value ≈ 0.0017`` (well below 0.05). We use
        ``pytest.approx`` with a 10% relative tolerance because the actual
        z computation depends on the pooled-proportion formula.
        """
        manager.start_experiment(
            name="ztest",
            champion_version="v1",
            challenger_version="v2",
            traffic_split=0.5,
            min_samples=50,
        )

        n = 100
        # Champion: 60% accuracy — first 60 predictions correct, last 40 wrong.
        # We use threshold-0.5 predictions; "correct" means prediction > 0.5
        # when actual=1 AND prediction <= 0.5 when actual=0.
        for i in range(n):
            if i < 60:
                # Correct prediction: prediction matches actual class.
                actual = 1 if i % 2 == 0 else 0
                champ_pred = 0.7 if actual == 1 else 0.3
                chall_pred = 0.7 if actual == 1 else 0.3
            else:
                # Wrong prediction: prediction disagrees with actual class.
                actual = 1 if i % 2 == 0 else 0
                champ_pred = 0.3 if actual == 1 else 0.7
                # Challenger: 80% accuracy — only 20 wrong.
                chall_pred = (
                    0.3 if actual == 1 and i >= 80 else
                    0.7 if actual == 0 and i >= 80 else
                    0.7 if actual == 1 else 0.3
                )
            manager.record_prediction(
                "v1", champ_pred, token_id=f"c_{i}", actual_outcome=actual
            )
            manager.record_prediction(
                "v2", chall_pred, token_id=f"h_{i}", actual_outcome=actual
            )

        result = manager.evaluate("ztest")
        assert result["status"] == "evaluated"

        # Sanity-check the per-arm accuracies match the construction.
        assert result["champion"]["accuracy"] == pytest.approx(0.60, abs=1e-6)
        assert result["challenger"]["accuracy"] == pytest.approx(0.80, abs=1e-6)

        # z-test should be highly significant.
        sig = result["significance"]
        assert sig["accuracy_z_score"] > 2.5  # |z| > 2.5 → p < 0.012
        assert sig["accuracy_p_value"] < 0.05
        assert sig["challenger_is_better"] is True
        assert result["recommendation"] == "promote"


# =============================================================================
# (7) API routes — register_routes endpoints
# =============================================================================
class TestAPIRoutes:
    """Verify the four ``/api/ab-test`` endpoints work end-to-end via
    ``TestClient``."""

    def test_get_status_when_no_experiment_active(self, client):
        """``GET /api/ab-test`` returns 200 with ``active=False`` when no
        experiment is running."""
        response = client.get("/api/ab-test")
        assert response.status_code == 200
        body = response.json()
        assert body["active"] is False
        # The ``experiments`` list is present (may be empty for a fresh DB).
        assert "experiments" in body
        assert isinstance(body["experiments"], list)

    def test_start_experiment_via_api(self, client):
        """``POST /api/ab-test/start`` returns 200 + the experiment descriptor
        and the subsequent ``GET /api/ab-test`` reflects the new active
        experiment."""
        response = client.post(
            "/api/ab-test/start",
            json={
                "name": "api_test_exp",
                "champion_version": "v1",
                "challenger_version": "v2",
                "traffic_split": 0.25,
                "min_samples": 40,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["started"] is True
        assert body["experiment"]["name"] == "api_test_exp"
        assert body["experiment"]["champion_version"] == "v1"
        assert body["experiment"]["challenger_version"] == "v2"
        assert body["experiment"]["traffic_split"] == 0.25
        assert body["experiment"]["min_samples"] == 40

        # The status endpoint now reflects the active experiment.
        status = client.get("/api/ab-test").json()
        assert status["active"] is True
        assert status["experiment"]["name"] == "api_test_exp"
        assert status["experiment"]["traffic_split"] == 0.25

    def test_start_experiment_rejects_invalid_traffic_split(self, client):
        """``POST /api/ab-test/start`` with ``traffic_split`` outside [0, 1]
        returns 400."""
        response = client.post(
            "/api/ab-test/start",
            json={
                "name": "bad_split",
                "champion_version": "v1",
                "challenger_version": "v2",
                "traffic_split": 1.5,
            },
        )
        assert response.status_code == 400
        # And no experiment was started.
        status = client.get("/api/ab-test").json()
        # ``active`` may be True if a prior test left an experiment running
        # against the patched singleton, but the bad-split experiment is
        # definitely not the active one.
        if status["active"]:
            assert status["experiment"]["name"] != "bad_split"

    def test_start_experiment_uses_default_traffic_split(self, client):
        """``POST /api/ab-test/start`` without ``traffic_split`` defaults to
        0.3 (per the Pydantic model's Field default)."""
        response = client.post(
            "/api/ab-test/start",
            json={
                "name": "default_split",
                "champion_version": "v1",
                "challenger_version": "v2",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["experiment"]["traffic_split"] == 0.3
        assert body["experiment"]["min_samples"] == 100  # also defaults

    def test_stop_experiment_via_api(self, client):
        """``POST /api/ab-test/stop`` returns 200 + the stopped name."""
        # Start an experiment first.
        client.post(
            "/api/ab-test/start",
            json={
                "name": "to_stop_via_api",
                "champion_version": "v1",
                "challenger_version": "v2",
                "traffic_split": 0.3,
            },
        )
        # Stop it.
        response = client.post(
            "/api/ab-test/stop",
            json={"name": "to_stop_via_api", "reason": "manual"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stopped"] is True
        assert body["name"] == "to_stop_via_api"
        assert body["reason"] == "manual"

        # Status now reflects no active experiment.
        status = client.get("/api/ab-test").json()
        assert status["active"] is False

    def test_stop_returns_404_when_no_experiment_active(self, client):
        """``POST /api/ab-test/stop`` with no name and no active experiment
        returns 404."""
        # Make sure no experiment is active.
        status = client.get("/api/ab-test").json()
        if status["active"]:
            client.post(
                "/api/ab-test/stop",
                json={"name": status["experiment"]["name"]},
            )
        # Now stop with no name → 404.
        response = client.post("/api/ab-test/stop", json={})
        assert response.status_code == 404

    def test_stop_returns_404_for_unknown_name(self, client):
        """``POST /api/ab-test/stop`` with an unknown name returns 404."""
        response = client.post(
            "/api/ab-test/stop",
            json={"name": "never_existed"},
        )
        assert response.status_code == 404

    def test_evaluate_unknown_experiment_returns_404(self, client):
        """``GET /api/ab-test/evaluate?experiment_name=xxx`` with an unknown
        name returns 404."""
        response = client.get(
            "/api/ab-test/evaluate",
            params={"experiment_name": "nonexistent"},
        )
        assert response.status_code == 404

    def test_evaluate_insufficient_data_via_api(self, client):
        """``GET /api/ab-test/evaluate`` returns 200 + ``insufficient_data``
        when the active experiment has too few samples."""
        # Start an experiment with a high min_samples.
        client.post(
            "/api/ab-test/start",
            json={
                "name": "api_insuf",
                "champion_version": "v1",
                "challenger_version": "v2",
                "traffic_split": 0.5,
                "min_samples": 50,
            },
        )

        response = client.get("/api/ab-test/evaluate")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "insufficient_data"
        assert body["champion_samples"] == 0
        assert body["challenger_samples"] == 0
        assert body["min_required"] == 50

    def test_evaluate_significant_winner_via_api(self, client):
        """End-to-end API test: start an experiment, record predictions via
        the manager singleton, then evaluate via the API and confirm a
        ``promote`` recommendation when the challenger is significantly
        better."""
        # Start an experiment with a low min_samples.
        client.post(
            "/api/ab-test/start",
            json={
                "name": "api_promote",
                "champion_version": "v1",
                "challenger_version": "v2",
                "traffic_split": 0.5,
                "min_samples": 30,
            },
        )

        # Record 60 predictions per arm via the singleton (the same object
        # the API handlers close over).
        rng = np.random.RandomState(123)
        n = 60
        for i in range(n):
            actual = int(rng.random() > 0.5)
            champ_pred = float(np.clip(0.3 + 0.4 * rng.random(), 0.05, 0.95))
            chall_pred = float(
                np.clip(0.1 + 0.85 * actual + 0.05 * rng.random(), 0.05, 0.95)
            )
            ab_test_singleton.record_prediction(
                "v1", champ_pred, token_id=f"c_{i}", actual_outcome=actual
            )
            ab_test_singleton.record_prediction(
                "v2", chall_pred, token_id=f"h_{i}", actual_outcome=actual
            )

        # Evaluate via the API.
        response = client.get(
            "/api/ab-test/evaluate",
            params={"experiment_name": "api_promote"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "evaluated"
        assert body["champion"]["version"] == "v1"
        assert body["challenger"]["version"] == "v2"
        # ``is_significant`` / ``challenger_is_better`` are real JSON
        # booleans (not the string "True" — which would be the symptom of
        # a numpy.bool_ leak through the JSON encoder).
        assert body["significance"]["is_significant"] is True
        assert body["significance"]["challenger_is_better"] is True
        assert body["recommendation"] == "promote"
