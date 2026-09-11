"""
tests/test_feature_store.py — Unit + integration tests for ``ml/feature_store.py``.

W16-2 — ML feature store (definitions / values / per-version importance /
windowed statistics / drift detection).

Covers the seven behaviours required by the W16-2 task spec:

  (1) ``register_feature`` ADDS a new feature to the definitions table —
      after a single call, ``get_all_features`` returns a list with
      one entry whose ``name`` / ``type`` / ``description`` match the
      caller's input.

  (2) ``record_values`` persists one row per numeric feature value —
      after ``record_values(token_id, {"a": 1.0, "b": 2.0, "c": 3.0})``,
      ``compute_stats("a")`` reports ``n_samples=1`` and ``mean=1.0``
      (and likewise for ``b`` and ``c``).

  (3) ``record_importance`` persists one row per feature, sorted by
      descending importance — the highest-importance feature gets
      ``rank=1``. Subsequent calls for a different ``model_version``
      create a second lineage so ``get_importance_history(model_version=...)``
      filters correctly.

  (4) ``compute_stats`` returns the canonical mean / std / min / max /
      p25 / p50 / p75 / p95 / n_samples dictionary over a windowed
      ``feature_values`` slice. Returns ``n_samples=0`` (no other keys)
      when the feature has no rows in the window.

  (5) ``get_top_features(model_version, top_n)`` returns the top-N
      importance rows for the given version, sorted by ascending rank.
      Asking for fewer than the catalog size returns only the top-N.

  (6) ``detect_feature_drift`` returns ``"insufficient_data"`` when
      either window has fewer than 10 samples, ``"stable"`` when the
      mean shift is below 0.5σ, and ``"drifted"`` when the mean shift
      exceeds 0.5σ. Constructed by injecting the same feature with
      different distributions in the two windows.

  (7) The five HTTP endpoints under ``/api/features`` work end-to-end
      via ``TestClient`` — ``GET /api/features`` returns 200 + the
      definitions list; ``GET /api/features/{name}/stats`` returns 200
      + the windowed stats (and 404 for an unknown feature);
      ``GET /api/features/importance`` returns 200 + the importance
      history (filterable by ``model_version`` / ``feature_name`` /
      ``limit``); ``GET /api/features/drift`` returns 200 + the drift
      status for every registered feature; ``POST /api/features/importance``
      returns 200 + ``{"recorded": N}`` after persisting the snapshot.

Module isolation
----------------
``ml/feature_store.py`` is pure-Python + synchronous at the manager
layer. The SQLite store is hermetic per-test via a fresh
``tmp_path``-scoped DB file passed to the ``FeatureStore(db_path=...)``
constructor — production's module-level singleton ``feature_store``
(constructed at import time against ``FEATURE_STORE_DB``, redirected by
``tests/conftest.py`` to ``/tmp/pmbot_conftest_isolation/feature_store.db``)
is left untouched by the unit tests.

For the API integration tests, the singleton IS used (the
``register_routes`` handlers close over the module-level ``feature_store``
singleton), so the test patches the singleton's ``_db_path`` to a fresh
``tmp_path``-scoped file via a direct attribute mutation + ``_init_db``
re-invocation (mirrors the ``test_ab_testing.py`` pattern). Teardown via
``request.addfinalizer`` restores the singleton to its conftest-default
path so any later test in the session that uses the singleton sees a
clean conftest-default DB.

Sync ``def`` tests throughout — ``TestClient``'s sync portal manages
the FastAPI event loop. No ``pytestmark = pytest.mark.asyncio`` is
needed (mirrors ``tests/test_ab_testing.py``,
``tests/test_live_safety_gate_api.py`` sync-test convention).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Bootstrap project root on sys.path (defensive; conftest.py also does this). ──
# Lets this file be run in isolation via
# ``python -m pytest tests/test_feature_store.py`` — the project root is
# always importable as top-level modules (``ml.*``) regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (sys.path must be set first)
import pytest  # noqa: E402  (sys.path must be set first)

from ml.feature_store import (  # noqa: E402
    FeatureDefinition,
    FeatureImportance,
    FeatureStore,
    feature_store as feature_store_singleton,
    register_routes,
)


# ── Fixture: fresh isolated FeatureStore per unit test ────────────────────────
@pytest.fixture
def store(tmp_path) -> FeatureStore:
    """Return a brand-new ``FeatureStore`` whose SQLite file lives under
    ``tmp_path``.

    Each test gets a clean DB (empty ``feature_definitions`` /
    ``feature_values`` / ``feature_importance`` / ``feature_stats``
    tables) so the module-level singleton ``feature_store`` (also
    constructed at import time and shared across the whole pytest
    session) is never perturbed by these unit tests.
    """
    db_path = tmp_path / "feature_store.db"
    return FeatureStore(db_path=db_path)


# ── Fixture: FastAPI TestClient with isolated singleton for API tests ────────
@pytest.fixture
def client(tmp_path, request):
    """Return a ``TestClient`` against a minimal FastAPI app with only the
    feature-store routes registered AND the module-level singleton
    ``feature_store`` patched to use a fresh ``tmp_path``-scoped SQLite
    file.

    The patch is applied by replacing the singleton's ``_db_path`` and
    re-running ``_init_db`` so the on-disk DB is a clean file with the
    fresh schema. ``request.addfinalizer`` restores the singleton to its
    conftest-default state on teardown (re-init against the
    ``FEATURE_STORE_DB`` env var so any later test in the session that
    uses the singleton sees a clean conftest-default DB).

    Mirrors the ``client`` fixture pattern in ``tests/test_ab_testing.py``
    — a per-test FastAPI app with ONLY the routes under test registered,
    so there's zero state leakage between tests.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def _restore_singleton():
        feature_store_singleton._db_path = Path(
            os.environ.get(
                "FEATURE_STORE_DB",
                "/tmp/pmbot_conftest_isolation/feature_store.db",
            )
        )
        feature_store_singleton._init_db()

    request.addfinalizer(_restore_singleton)

    # ── Patch the module-level singleton to a fresh tmp_path-scoped DB ──
    # ``feature_store_singleton`` is the same object reference the
    # ``register_routes`` handlers close over (via the module's globals);
    # mutating its ``_db_path`` + re-running ``_init_db`` is enough —
    # the handlers' name lookup resolves the same object at runtime.
    db_path = tmp_path / "api_feature_store.db"
    feature_store_singleton._db_path = db_path
    feature_store_singleton._init_db()

    app = FastAPI()
    register_routes(app)
    return TestClient(app)


# =============================================================================
# (1) register_feature adds a feature to the catalog
# =============================================================================
class TestRegisterFeature:
    """Verify ``register_feature`` correctly persists a feature definition."""

    def test_register_adds_one_feature_to_catalog(self, store):
        """After a single ``register_feature`` call, ``get_all_features``
        returns a list with one entry whose ``name`` / ``type`` /
        ``description`` match the caller's input."""
        store.register_feature(
            name="mid_price",
            type="numeric",
            description="Top-of-book midpoint probability",
        )

        features = store.get_all_features()
        assert isinstance(features, list)
        assert len(features) == 1, (
            f"Expected 1 feature, got {len(features)}: {features}"
        )

        f = features[0]
        assert f["name"] == "mid_price"
        assert f["type"] == "numeric"
        assert f["description"] == "Top-of-book midpoint probability"
        # ``created_at`` is a float epoch timestamp populated by default.
        assert isinstance(f["created_at"], float)
        assert f["created_at"] > 0

    def test_register_is_idempotent_on_name_collision(self, store):
        """Re-registering the same name UPSERTS the row (does not insert
        a duplicate) — ``get_all_features`` still returns one entry."""
        store.register_feature("foo", "numeric", "first")
        store.register_feature("foo", "numeric", "second")

        features = store.get_all_features()
        assert len(features) == 1
        assert features[0]["description"] == "second"

    def test_register_persists_min_max_bounds(self, store):
        """``min_value`` and ``max_value`` are persisted (not silently
        dropped) when supplied."""
        store.register_feature(
            "bounded",
            "numeric",
            "bounded feature",
            min_value=0.0,
            max_value=1.0,
        )
        features = store.get_all_features()
        assert len(features) == 1
        assert features[0]["min_value"] == 0.0
        assert features[0]["max_value"] == 1.0


# =============================================================================
# (2) record_values persists one row per numeric feature value
# =============================================================================
class TestRecordValues:
    """Verify ``record_values`` correctly persists per-prediction value rows."""

    def test_record_values_persists_one_row_per_numeric_feature(self, store):
        """After ``record_values(token_id, {"a": 1.0, "b": 2.0, "c": 3.0})``,
        each of the three features has exactly one value row in the DB
        and ``compute_stats`` reflects those single-sample values."""
        store.register_feature("a", "numeric")
        store.register_feature("b", "numeric")
        store.register_feature("c", "numeric")

        store.record_values(
            token_id="tok1",
            features={"a": 1.0, "b": 2.0, "c": 3.0},
            prediction_id="pred-1",
        )

        # Each feature sees exactly one value.
        assert store.compute_stats("a")["n_samples"] == 1
        assert store.compute_stats("b")["n_samples"] == 1
        assert store.compute_stats("c")["n_samples"] == 1
        assert store.compute_stats("a")["mean"] == pytest.approx(1.0)
        assert store.compute_stats("b")["mean"] == pytest.approx(2.0)
        assert store.compute_stats("c")["mean"] == pytest.approx(3.0)

    def test_record_values_skips_non_numeric_entries(self, store):
        """``record_values`` writes a row ONLY for entries whose value is
        a Python int / float — strings / None / lists are silently
        skipped (the spec's defensive ``isinstance`` guard)."""
        store.register_feature("numeric", "numeric")
        store.record_values(
            token_id="tok-mixed",
            features={
                "numeric": 0.5,
                "string_val": "not-numeric",   # skipped
                "none_val": None,              # skipped
                "list_val": [1.0, 2.0],        # skipped
            },
        )

        stats = store.compute_stats("numeric")
        assert stats["n_samples"] == 1
        assert stats["mean"] == pytest.approx(0.5)

    def test_record_values_supports_int_and_float(self, store):
        """Both ``int`` and ``float`` values are persisted as floats
        (no implicit type assumption)."""
        store.register_feature("int_feat", "numeric")
        store.register_feature("float_feat", "numeric")

        store.record_values(
            token_id="tok-types",
            features={"int_feat": 7, "float_feat": 0.123},
        )

        assert store.compute_stats("int_feat")["mean"] == pytest.approx(7.0)
        assert store.compute_stats("float_feat")["mean"] == pytest.approx(0.123)

    def test_record_values_accumulates_across_calls(self, store):
        """Multiple ``record_values`` calls append rows — the per-feature
        sample count grows monotonically."""
        store.register_feature("feat", "numeric")

        for i in range(5):
            store.record_values(
                token_id=f"tok-{i}",
                features={"feat": float(i)},
            )

        stats = store.compute_stats("feat")
        assert stats["n_samples"] == 5
        # mean of [0, 1, 2, 3, 4] = 2.0
        assert stats["mean"] == pytest.approx(2.0)


# =============================================================================
# (3) record_importance persists per-version importance snapshots
# =============================================================================
class TestRecordImportance:
    """Verify ``record_importance`` correctly persists per-version
    feature-importance snapshots."""

    def test_record_importance_assigns_descending_rank(self, store):
        """``record_importance`` sorts the dict by descending importance
        and assigns ``rank=1`` to the highest, ``rank=2`` to the next,
        etc."""
        store.record_importance(
            model_version="v1.100.0",
            importance_dict={"a": 0.1, "b": 0.5, "c": 0.3},
        )

        top = store.get_top_features("v1.100.0", top_n=10)
        assert len(top) == 3
        # Ranks must be ascending (1, 2, 3).
        ranks = [r["rank"] for r in top]
        assert ranks == [1, 2, 3]
        # Top feature is ``b`` (importance 0.5), then ``c`` (0.3), then ``a`` (0.1).
        assert top[0]["feature_name"] == "b"
        assert top[0]["importance"] == pytest.approx(0.5)
        assert top[1]["feature_name"] == "c"
        assert top[1]["importance"] == pytest.approx(0.3)
        assert top[2]["feature_name"] == "a"
        assert top[2]["importance"] == pytest.approx(0.1)

    def test_record_importance_supports_multiple_versions(self, store):
        """``record_importance`` for two different model_versions creates
        two distinct lineages — ``get_top_features("v1")`` and
        ``get_top_features("v2")`` filter correctly."""
        store.record_importance("v1.100.0", {"a": 0.2, "b": 0.8})
        store.record_importance("v1.200.0", {"a": 0.7, "b": 0.3})

        top_v1 = store.get_top_features("v1.100.0", top_n=10)
        top_v2 = store.get_top_features("v1.200.0", top_n=10)

        assert len(top_v1) == 2
        assert len(top_v2) == 2

        # In v1.100.0, ``b`` was more important (0.8 > 0.2).
        assert top_v1[0]["feature_name"] == "b"
        # In v1.200.0, ``a`` was more important (0.7 > 0.3).
        assert top_v2[0]["feature_name"] == "a"

    def test_get_top_features_limits_to_top_n(self, store):
        """``get_top_features(model_version, top_n=2)`` returns only the
        two highest-importance features (the catalog may have more)."""
        store.record_importance(
            "v1.100.0",
            {"a": 0.1, "b": 0.5, "c": 0.3, "d": 0.05},
        )
        top = store.get_top_features("v1.100.0", top_n=2)
        assert len(top) == 2
        assert top[0]["feature_name"] == "b"
        assert top[1]["feature_name"] == "c"

    def test_get_importance_history_filters_by_feature_and_version(self, store):
        """``get_importance_history`` accepts ``feature_name`` and
        ``model_version`` filters and returns only the matching rows."""
        store.record_importance("v1.100.0", {"a": 0.2, "b": 0.8})
        store.record_importance("v1.200.0", {"a": 0.7, "b": 0.3})

        # Filter by feature_name only.
        a_history = store.get_importance_history(feature_name="a")
        assert len(a_history) == 2
        assert all(r["feature_name"] == "a" for r in a_history)

        # Filter by model_version only.
        v1_history = store.get_importance_history(model_version="v1.100.0")
        assert len(v1_history) == 2
        assert all(r["model_version"] == "v1.100.0" for r in v1_history)

        # Filter by both.
        both = store.get_importance_history(
            feature_name="b",
            model_version="v1.200.0",
        )
        assert len(both) == 1
        assert both[0]["feature_name"] == "b"
        assert both[0]["model_version"] == "v1.200.0"


# =============================================================================
# (4) compute_stats returns canonical statistics over a windowed slice
# =============================================================================
class TestComputeStats:
    """Verify ``compute_stats`` correctly computes windowed statistics."""

    def test_compute_stats_returns_zero_samples_for_unknown_feature(self, store):
        """``compute_stats`` for an unknown feature returns
        ``n_samples=0`` with NO other keys (per the spec's early-return
        branch)."""
        stats = store.compute_stats("nonexistent")
        assert stats == {"feature": "nonexistent", "n_samples": 0}

    def test_compute_stats_returns_full_stats_for_one_sample(self, store):
        """A single value yields ``mean=min=max=p25=p50=p75=p95=value``
        and ``std=0``."""
        store.register_feature("feat", "numeric")
        store.record_values("tok1", {"feat": 0.42})

        stats = store.compute_stats("feat")
        assert stats["n_samples"] == 1
        assert stats["mean"] == pytest.approx(0.42)
        assert stats["std"] == pytest.approx(0.0)
        assert stats["min"] == pytest.approx(0.42)
        assert stats["max"] == pytest.approx(0.42)
        for key in ("p25", "p50", "p75", "p95"):
            assert stats[key] == pytest.approx(0.42)

    def test_compute_stats_matches_numpy_on_known_distribution(self, store):
        """The stats dict matches a hand-computed numpy reference for a
        100-sample uniform distribution."""
        store.register_feature("feat", "numeric")
        rng = np.random.RandomState(42)
        values = rng.uniform(0.0, 1.0, 100)
        for v in values:
            store.record_values("tok1", {"feat": float(v)})

        stats = store.compute_stats("feat")
        assert stats["n_samples"] == 100
        assert stats["mean"] == pytest.approx(float(np.mean(values)))
        assert stats["std"] == pytest.approx(float(np.std(values)))
        assert stats["min"] == pytest.approx(float(np.min(values)))
        assert stats["max"] == pytest.approx(float(np.max(values)))
        assert stats["p25"] == pytest.approx(float(np.percentile(values, 25)))
        assert stats["p50"] == pytest.approx(float(np.percentile(values, 50)))
        assert stats["p75"] == pytest.approx(float(np.percentile(values, 75)))
        assert stats["p95"] == pytest.approx(float(np.percentile(values, 95)))

    def test_compute_stats_respects_since_hours_window(self, store):
        """``compute_stats(feature, since_hours=...)`` filters to rows
        newer than the cutoff. Old rows are excluded even if they exist."""
        store.register_feature("feat", "numeric")

        # Inject a row with an OLD timestamp directly via SQLite so we
        # don't have to wait 24h to test the window.
        import sqlite3
        old_ts = time.time() - 48 * 3600  # 48 hours ago
        with sqlite3.connect(store._db_path) as conn:
            conn.execute(
                "INSERT INTO feature_values (token_id, feature_name, value, timestamp, prediction_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("tok-old", "feat", 100.0, old_ts, None),
            )

        # A recent row (within the default 24h window).
        store.record_values("tok-recent", {"feat": 0.5})

        # Default 24h window → only the recent row counts.
        stats = store.compute_stats("feat", since_hours=24)
        assert stats["n_samples"] == 1
        assert stats["mean"] == pytest.approx(0.5)

        # 100h window → both rows count.
        stats_wide = store.compute_stats("feat", since_hours=100)
        assert stats_wide["n_samples"] == 2


# =============================================================================
# (5) get_top_features returns the top-N most important features
# =============================================================================
class TestGetTopFeatures:
    """Verify ``get_top_features`` returns top-N importance rows by rank."""

    def test_get_top_features_returns_empty_for_unknown_version(self, store):
        """``get_top_features`` for a version with no recorded importance
        returns an empty list (not None, not 404 — the SQL query simply
        yields no rows)."""
        top = store.get_top_features("never_recorded", top_n=10)
        assert top == []

    def test_get_top_features_orders_by_ascending_rank(self, store):
        """``get_top_features`` always returns rows in ascending rank
        order — ``rank=1`` first, then ``rank=2``, etc."""
        store.record_importance(
            "v1.100.0",
            # Construct an unsorted dict so the manager's internal sort
            # is exercised, not just an already-sorted input.
            {"f1": 0.05, "f2": 0.20, "f3": 0.50, "f4": 0.10, "f5": 0.15},
        )
        top = store.get_top_features("v1.100.0", top_n=10)
        ranks = [r["rank"] for r in top]
        assert ranks == sorted(ranks)
        # Top feature is ``f3`` (importance 0.50).
        assert top[0]["feature_name"] == "f3"
        assert top[0]["importance"] == pytest.approx(0.50)


# =============================================================================
# (6) detect_feature_drift returns drifted / stable / insufficient_data
# =============================================================================
class TestDetectFeatureDrift:
    """Verify ``detect_feature_drift`` correctly classifies drift status."""

    def test_drift_returns_insufficient_data_when_windows_too_small(self, store):
        """``detect_feature_drift`` returns ``"insufficient_data"`` when
        either the reference window or the current window has fewer than
        10 samples (the documented minimum for a meaningful mean-shift test)."""
        store.register_feature("feat", "numeric")
        # Inject only 5 recent samples (well under the 10-sample threshold).
        for i in range(5):
            store.record_values("tok", {"feat": float(i)})

        drift = store.detect_feature_drift("feat")
        assert drift["status"] == "insufficient_data"
        assert drift["feature"] == "feat"

    def test_drift_returns_stable_when_mean_shift_below_half_sigma(self, store):
        """``detect_feature_drift`` returns ``"stable"`` when the mean
        shift between the current and reference windows is below 0.5σ."""
        store.register_feature("feat", "numeric")
        # Reference window (168h): normal distribution mean=0, std≈1.
        # Current window (24h): same distribution → mean shift ≈ 0σ.
        # We inject 50 samples with mean 0 in the reference window, then
        # 50 samples with mean 0 in the current window. Both windows
        # cover ≥ 10 samples so the early-return branch is bypassed.
        rng = np.random.RandomState(42)
        # Reference window: 50 samples, normally distributed around 0.
        ref_values = rng.normal(0.0, 1.0, 50)
        # We need these to be older than 24h so they don't show up in the
        # current window. Inject with an explicit older timestamp.
        import sqlite3
        old_ts = time.time() - 48 * 3600
        with sqlite3.connect(store._db_path) as conn:
            for v in ref_values:
                conn.execute(
                    "INSERT INTO feature_values (token_id, feature_name, value, timestamp, prediction_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("tok-ref", "feat", float(v), old_ts, None),
                )
        # Current window: 50 samples, normally distributed around 0.1
        # → mean shift ≈ 0.1σ (well below the 0.5σ threshold).
        for v in rng.normal(0.1, 1.0, 50):
            store.record_values("tok-cur", {"feat": float(v)})

        drift = store.detect_feature_drift("feat")
        assert drift["status"] == "stable", (
            f"Expected 'stable' for small mean shift, got {drift}"
        )
        assert drift["mean_shift_sigma"] < 0.5
        assert drift["reference_samples"] >= 10
        assert drift["current_samples"] >= 10

    def test_drift_returns_drifted_when_mean_shift_exceeds_half_sigma(self, store):
        """``detect_feature_drift`` returns ``"drifted"`` when the mean
        shift between the current and reference windows exceeds 0.5σ.

        Note: the reference window (168h) is a SUPERSET of the current
        window (24h) — drift is computed as
        ``|current_mean - reference_mean| / reference_std`` where the
        reference window includes the current window. So when the recent
        50 samples are shifted by +2σ and the older 50 samples are at
        mean=0, the reference mean lands at ≈1.0 (the average of the two
        clusters) and the current mean lands at ≈2.0 — yielding a mean
        shift of ≈1.0σ, comfortably above the 0.5σ threshold.
        """
        store.register_feature("feat", "numeric")
        # Reference window (168h): mean=0, std≈1 (50 samples, older than 24h).
        rng = np.random.RandomState(7)
        ref_values = rng.normal(0.0, 1.0, 50)
        import sqlite3
        old_ts = time.time() - 48 * 3600
        with sqlite3.connect(store._db_path) as conn:
            for v in ref_values:
                conn.execute(
                    "INSERT INTO feature_values (token_id, feature_name, value, timestamp, prediction_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("tok-ref", "feat", float(v), old_ts, None),
                )
        # Current window: mean=2, std≈1 → mean shift ≈ 1σ (well above 0.5σ).
        for v in rng.normal(2.0, 1.0, 50):
            store.record_values("tok-cur", {"feat": float(v)})

        drift = store.detect_feature_drift("feat")
        assert drift["status"] == "drifted", (
            f"Expected 'drifted' for large mean shift, got {drift}"
        )
        assert drift["mean_shift_sigma"] > 0.5
        # Reference mean lands at ≈1.0 (mix of the old 0-centered cluster
        # and the recent 2-centered cluster, since the reference window
        # includes the current window). Current mean is ≈2.0.
        assert drift["reference_mean"] == pytest.approx(1.0, abs=0.3)
        assert drift["current_mean"] == pytest.approx(2.0, abs=0.3)


# =============================================================================
# (7) API routes — register_routes endpoints
# =============================================================================
class TestAPIRoutes:
    """Verify the five ``/api/features`` endpoints work end-to-end via
    ``TestClient``."""

    def test_get_features_returns_empty_list_when_nothing_registered(self, client):
        """``GET /api/features`` returns 200 with an empty ``features``
        list when no features have been registered yet."""
        response = client.get("/api/features")
        assert response.status_code == 200
        body = response.json()
        assert body["features"] == []

    def test_get_features_returns_registered_definitions(self, client):
        """``GET /api/features`` returns 200 + the registered feature
        definitions after ``register_feature`` is called via the singleton."""
        feature_store_singleton.register_feature(
            "mid_price", "numeric", "Top-of-book midpoint",
        )
        feature_store_singleton.register_feature(
            "spread_norm", "numeric", "Normalized bid-ask spread",
        )
        response = client.get("/api/features")
        assert response.status_code == 200
        features = response.json()["features"]
        assert len(features) == 2
        names = {f["name"] for f in features}
        assert names == {"mid_price", "spread_norm"}

    def test_get_feature_stats_returns_404_for_unknown_feature(self, client):
        """``GET /api/features/{name}/stats`` returns 404 when the feature
        name is not registered."""
        response = client.get("/api/features/nonexistent/stats")
        assert response.status_code == 404

    def test_get_feature_stats_returns_windowed_stats(self, client):
        """``GET /api/features/{name}/stats`` returns 200 + the windowed
        statistics (mean / std / min / max / percentiles / n_samples)
        merged with the feature-definition metadata."""
        feature_store_singleton.register_feature(
            "mid_price", "numeric", "Top-of-book midpoint",
            min_value=0.0, max_value=1.0,
        )
        # Inject a few values.
        for v in (0.4, 0.5, 0.6):
            feature_store_singleton.record_values("tok", {"mid_price": v})

        response = client.get("/api/features/mid_price/stats")
        assert response.status_code == 200
        body = response.json()
        # The definition metadata is echoed back.
        assert body["name"] == "mid_price"
        assert body["type"] == "numeric"
        assert body["min_value"] == 0.0
        assert body["max_value"] == 1.0
        # The windowed statistics are present.
        assert body["n_samples"] == 3
        assert body["mean"] == pytest.approx(0.5)
        assert body["min"] == pytest.approx(0.4)
        assert body["max"] == pytest.approx(0.6)

    def test_get_feature_stats_respects_since_hours_param(self, client):
        """``GET /api/features/{name}/stats?since_hours=...`` filters
        the window to the requested number of hours."""
        feature_store_singleton.register_feature("feat", "numeric")
        # Inject an OLD row directly via SQLite (48h ago) so we can test
        # the 24h default window exclusion without waiting.
        import sqlite3
        old_ts = time.time() - 48 * 3600
        with sqlite3.connect(feature_store_singleton._db_path) as conn:
            conn.execute(
                "INSERT INTO feature_values (token_id, feature_name, value, timestamp, prediction_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("tok-old", "feat", 100.0, old_ts, None),
            )
        # A recent row.
        feature_store_singleton.record_values("tok-recent", {"feat": 0.5})

        # Default 24h window → only the recent row counts.
        r24 = client.get("/api/features/feat/stats")
        assert r24.status_code == 200
        assert r24.json()["n_samples"] == 1
        assert r24.json()["mean"] == pytest.approx(0.5)

        # 100h window → both rows count.
        r100 = client.get("/api/features/feat/stats?since_hours=100")
        assert r100.status_code == 200
        assert r100.json()["n_samples"] == 2

    def test_get_importance_history_empty(self, client):
        """``GET /api/features/importance`` returns 200 + an empty
        ``history`` list when nothing has been recorded."""
        response = client.get("/api/features/importance")
        assert response.status_code == 200
        body = response.json()
        assert body["history"] == []

    def test_get_importance_history_filters_by_version(self, client):
        """``GET /api/features/importance?model_version=v1.100.0`` filters
        the returned history to a single version."""
        feature_store_singleton.record_importance("v1.100.0", {"a": 0.7, "b": 0.3})
        feature_store_singleton.record_importance("v1.200.0", {"a": 0.4, "b": 0.6})

        # No filter → all 4 rows returned.
        r_all = client.get("/api/features/importance?limit=100")
        assert r_all.status_code == 200
        assert len(r_all.json()["history"]) == 4

        # Filter by v1.100.0 → only its 2 rows.
        r_v1 = client.get("/api/features/importance?model_version=v1.100.0")
        assert r_v1.status_code == 200
        history = r_v1.json()["history"]
        assert len(history) == 2
        assert all(h["model_version"] == "v1.100.0" for h in history)

    def test_get_importance_history_filters_by_feature_name(self, client):
        """``GET /api/features/importance?feature_name=a`` filters the
        returned history to a single feature."""
        feature_store_singleton.record_importance("v1.100.0", {"a": 0.7, "b": 0.3})
        feature_store_singleton.record_importance("v1.200.0", {"a": 0.4, "b": 0.6})

        r = client.get("/api/features/importance?feature_name=a&limit=100")
        assert r.status_code == 200
        history = r.json()["history"]
        assert len(history) == 2
        assert all(h["feature_name"] == "a" for h in history)

    def test_get_importance_history_respects_limit(self, client):
        """``GET /api/features/importance?limit=N`` returns at most N rows."""
        feature_store_singleton.record_importance(
            "v1.100.0",
            {f"f{i}": float(i) for i in range(20)},
        )
        r = client.get("/api/features/importance?limit=5")
        assert r.status_code == 200
        assert len(r.json()["history"]) == 5

    def test_get_drift_returns_drifts_array_with_one_entry_per_feature(self, client):
        """``GET /api/features/drift`` returns 200 + a ``drifts`` list
        with one entry per registered feature (``status`` defaults to
        ``insufficient_data`` when no values have been recorded)."""
        feature_store_singleton.register_feature("feat_a", "numeric")
        feature_store_singleton.register_feature("feat_b", "numeric")

        response = client.get("/api/features/drift")
        assert response.status_code == 200
        body = response.json()
        assert body["n_features"] == 2
        assert len(body["drifts"]) == 2
        # No values recorded yet → insufficient_data.
        for d in body["drifts"]:
            assert d["status"] == "insufficient_data"

    def test_post_importance_records_snapshot(self, client):
        """``POST /api/features/importance`` returns 200 + ``{"recorded": N}``
        after persisting the snapshot, and the snapshot is then retrievable
        via ``GET /api/features/importance``."""
        response = client.post(
            "/api/features/importance",
            json={
                "model_version": "v1.999.0",
                "importance": {"a": 0.4, "b": 0.6},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["recorded"] == 2

        # Verify the snapshot is now in the history.
        history = client.get(
            "/api/features/importance?model_version=v1.999.0",
        ).json()["history"]
        assert len(history) == 2
        # Ranks: b (0.6) → 1, a (0.4) → 2.
        rank_by_feature = {h["feature_name"]: h["rank"] for h in history}
        assert rank_by_feature["b"] == 1
        assert rank_by_feature["a"] == 2

    def test_post_importance_validates_request_body(self, client):
        """``POST /api/features/importance`` returns 422 when the body
        is missing required fields (``model_version`` or ``importance``)."""
        # Missing model_version.
        r1 = client.post(
            "/api/features/importance",
            json={"importance": {"a": 0.5}},
        )
        assert r1.status_code == 422

        # Missing importance dict.
        r2 = client.post(
            "/api/features/importance",
            json={"model_version": "v1.0.0"},
        )
        assert r2.status_code == 422

        # Empty body.
        r3 = client.post("/api/features/importance", json={})
        assert r3.status_code == 422


# =============================================================================
# Dataclass + integration smoke tests
# =============================================================================
class TestDataclassesAndIntegration:
    """Verify the dataclasses can be constructed and that the
    ``FeatureStore`` module exposes a singleton ``feature_store`` and
    a ``register_routes`` function (so the ``api/server.py`` wiring
    can find them at import time)."""

    def test_feature_definition_dataclass_construction(self):
        """``FeatureDefinition`` is a dataclass with the documented fields
        and a ``created_at`` default."""
        fd = FeatureDefinition(
            name="test",
            type="numeric",
            description="test feature",
        )
        assert fd.name == "test"
        assert fd.type == "numeric"
        assert fd.description == "test feature"
        assert fd.min_value is None
        assert fd.max_value is None
        assert fd.created_at > 0  # default_factory=time.time

    def test_feature_importance_dataclass_construction(self):
        """``FeatureImportance`` is a dataclass with the documented fields."""
        fi = FeatureImportance(
            feature_name="test",
            model_version="v1.0.0",
            importance=0.5,
            rank=1,
            timestamp=time.time(),
        )
        assert fi.feature_name == "test"
        assert fi.model_version == "v1.0.0"
        assert fi.importance == 0.5
        assert fi.rank == 1
        assert fi.timestamp > 0

    def test_module_level_singleton_exists(self):
        """``feature_store`` is a module-level singleton constructed at
        import time (mirrors the ``ab_test`` singleton pattern in
        ``ml/ab_testing.py``)."""
        assert isinstance(feature_store_singleton, FeatureStore)
        # The singleton's _db_path should be set (either to the conftest
        # redirect or to the production /app/data default).
        assert feature_store_singleton._db_path is not None

    def test_register_routes_is_callable(self):
        """``register_routes`` is a callable that accepts a FastAPI app."""
        assert callable(register_routes)
        # Registering against a fresh FastAPI app should not raise.
        from fastapi import FastAPI
        app = FastAPI()
        register_routes(app)
        # The five feature-store endpoints should now be on the app's
        # route table.
        paths = {r.path for r in app.routes if getattr(r, "path", None)}
        assert "/api/features" in paths
        assert "/api/features/importance" in paths
        assert "/api/features/drift" in paths
        assert "/api/features/{name}/stats" in paths
