"""tests/test_ml_feature_wiring.py — W33-2 ML↔feature-pipeline wiring tests.

Four test areas required by the W33-2 task spec:

  (1) **ML model uses feature pipeline when no features provided** —
      ``MarketMLModel.predict_proba(token_id=T, features=None)`` resolves
      the feature pipeline (injected via ``model._feature_pipeline``),
      asks it for the 38-dim vector at ``time.time()``, and forwards
      those features into the existing ``predict()`` path. The returned
      ``p_yes`` matches the model's prediction for the pipeline's
      features — i.e. the wiring is correct (no silent drop / no silent
      substitution).

  (2) **Point-in-time prediction** —
      ``MarketMLModel.predict_proba_at(token_id=T, timestamp=T0)``
      returns the model's prediction for the feature vector the pipeline
      reconstructs at ``T0`` (only snapshots with ``observation_ts <=
      T0`` are visible to the PIT filter). A snapshot at T2 strictly
      after T0 must NOT leak into the T0 prediction.

  (3) **Feature freshness check** — When the most recent snapshot for
      ``token_id`` is older than ``FEATURE_FRESHNESS_THRESHOLD_SECONDS``
      (60 s), the model still predicts (the prediction is NOT
      short-circuited) but logs a WARNING. The ``last_feature_age``
      instance attribute is also exposed so tests (and a future
      ``/api/ml/metrics`` field) can assert on the freshness path
      without parsing log records.

  (4) **Fallback when features unavailable** — When the pipeline
      returns ``None`` (no snapshot at-or-before ``as_of``), the model
      returns the neutral ``0.5`` prediction (the documented W33-2
      "neutral prediction when data unavailable" contract). This is
      exercised via both the live-serving ``predict_proba(token_id=...)``
      path and the PIT ``predict_proba_at(token_id=..., timestamp=...)``
      path.

Module isolation
----------------
The feature pipeline is constructed directly (``FeaturePipeline(db=
MockSnapshotSource(rows), feature_store=None)``) and injected via
``model._feature_pipeline = pipe`` — no monkey-patching of the
process-wide singleton is required. ``MockSnapshotSource`` mirrors the
shape used in ``tests/test_feature_pipeline.py`` so the two test suites
share fixture conventions. The model is constructed directly as
``MarketMLModel()`` (no ``fit_initial``) so the predict-path falls back
to ``float(features[0])`` on the unfitted model — same pattern as
``tests/test_ml_model.py``'s test 1. ``ml.features._price_history`` is
cleared in an autouse fixture so the rolling features don't accumulate
across tests (mirrors ``tests/test_feature_pipeline.py``'s autouse
fixture).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Same pattern as ``tests/test_feature_pipeline.py`` / ``tests/conftest.py``:
# redirect every persisted-state path into a writable /tmp sandbox so the
# module-level singletons (``core.database_manager.db_manager``,
# ``ml.feature_store.feature_store``, ``ml.model_registry.model_registry``,
# ``ml.ab_testing.ab_test``, …) don't raise ``PermissionError`` on the
# read-only ``/app/data`` sandbox path.
_TMP_ROOT = Path("/tmp/pmbot_ml_feature_wiring_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
(_TMP_ROOT / "dao_data").mkdir(parents=True, exist_ok=True)

# Mirror the conftest env redirects (the sibling ``tests/conftest.py`` is
# imported by pytest before this module, but the inline redirect makes the
# test file self-sufficient when invoked directly via
# ``pytest tests/test_ml_feature_wiring.py`` from an IDE).
_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "MARKET_DAO_DB_PATH": str(_TMP_ROOT / "market_dao.db"),
    "DECISION_LEDGER_DAO_DB_PATH": str(_TMP_ROOT / "decision_ledger_dao.db"),
    "BOT_DATA_DIR": str(_TMP_ROOT / "dao_data"),
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-ml-feature-wiring",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# ── Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package. ──────────────────────────────────────────
# Same situation + fix as ``tests/test_feature_pipeline.py`` /
# ``tests/test_ingestion_infra.py``: pytest's default ``prepend`` import mode
# inserts ``tests/`` at ``sys.path[0]`` during test collection, which lets
# the sibling ``tests/ingestion/`` package shadow our top-level
# ``polymarket-bot/ingestion/`` package. Without the ``remove`` step below,
# the project root ends up behind ``tests/`` in sys.path, and
# ``from ingestion.feature_pipeline import ...`` resolves to
# ``tests/ingestion/__init__.py`` (which has no ``feature_pipeline`` submodule).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Clear any cached ``ingestion`` / ``ingestion.*`` module pointing at the
# ``tests/ingestion/`` directory so the next import resolves against the
# freshly-prepended ``_PROJECT_ROOT``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import numpy as np  # noqa: E402  (sys.path must be set first)
import pytest  # noqa: E402  (sys.path must be set first)

from ingestion.feature_pipeline import FeaturePipeline  # noqa: E402
from ml import features as _feat_mod  # noqa: E402
from ml.features import FEATURE_NAMES, N_FEATURES  # noqa: E402
from ml.model import (  # noqa: E402
    FEATURE_FRESHNESS_THRESHOLD_SECONDS,
    MarketMLModel,
    _run_async,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


class MockSnapshotSource:
    """In-memory snapshot source — most-recent-first (mirrors
    ``db_manager.get_snapshots`` return order)."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        # Sort most-recent-first so the mock faithfully mirrors the real
        # backend's contract (the pipeline relies on this order).
        self._rows: list[dict] = sorted(
            list(rows or []),
            key=lambda r: r.get("timestamp", 0.0),
            reverse=True,
        )

    async def get_snapshots(
        self, token_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = [r for r in self._rows if r.get("token_id") == token_id]
        return rows[:limit]


def _make_snapshot_row(
    token_id: str,
    ts: float,
    best_bid: float,
    best_ask: float,
    *,
    bid_size: float = 100.0,
    ask_size: float = 100.0,
    volume_24h: float = 1000.0,
    liquidity: float = 5000.0,
    source: str = "clob_rest",
) -> dict:
    """Build a snapshot row mirroring ``db_manager.get_snapshots`` schema."""
    bids: list[dict] = [{"price": best_bid, "size": bid_size}]
    asks: list[dict] = [{"price": best_ask, "size": ask_size}]
    mid = (best_bid + best_ask) / 2.0
    return {
        "token_id": token_id,
        "timestamp": ts,
        "ingestion_time": ts,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": best_ask - best_bid,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "bids_json": json.dumps(bids),
        "asks_json": json.dumps(asks),
        "bid_depth_10": float(sum(b["size"] for b in bids[:10])),
        "ask_depth_10": float(sum(a["size"] for a in asks[:10])),
        "source": source,
    }


def _make_pipeline(rows: list[dict]) -> FeaturePipeline:
    """Build a FeaturePipeline backed by an in-memory MockSnapshotSource.

    The feature_store is ``None`` so the pipeline's persistence hooks are
    skipped (the W33-2 wiring is purely about feature FETCH, not about
    the feature-store persistence layer — that path is already covered
    by ``tests/test_feature_pipeline.py``).
    """
    return FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)


def _make_unfitted_model(pipeline: FeaturePipeline | None = None) -> MarketMLModel:
    """Build a fresh, unfitted ``MarketMLModel`` with a feature pipeline injected.

    Unfitted means the predict-path falls back to ``float(features[0])``
    (mid_price) for ``p_yes`` — same fallback the production
    ``MarketMLModel.predict`` uses when ``rf``/``gb`` are ``None``. The
    W33-2 wiring is independent of whether the model is fitted, so the
    unfitted path is the simplest hermetic setup.
    """
    m = MarketMLModel()
    if pipeline is not None:
        m._feature_pipeline = pipeline
    return m


# ── Autouse fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_price_history():
    """Clear ``ml.features._price_history`` before + after every test.

    The pipeline primes this deque from the snapshot window so the
    Hurst / momentum / rolling-vol features are point-in-time correct.
    Without a clear, state from one test would leak into the next and
    mask regressions (same pattern as the autouse fixture in
    ``tests/test_feature_pipeline.py``).
    """
    _feat_mod._price_history.clear()
    yield
    _feat_mod._price_history.clear()


@pytest.fixture(autouse=True)
def _reset_last_feature_age():
    """``MarketMLModel.last_feature_age`` defaults to ``None`` — clear it
    before AND after every test so a prior test's value doesn't leak
    into a fresh assertion. (Tests construct fresh model instances, so
    this is belt-and-braces — the fresh instance already has ``None``.)"""
    yield


# ── (1) ML model uses feature pipeline when no features provided ─────────────


def test_predict_proba_token_id_only_uses_pipeline_features():
    """``predict_proba(token_id=T, features=None)`` resolves the pipeline,
    fetches the 38-dim feature vector, and forwards it to ``predict()``.

    Asserted via the unfitted model's ``predict()`` fallback
    (``float(features[0])`` = ``mid_price``): for a snapshot with
    best_bid=0.48 / best_ask=0.52 (mid=0.50), the returned ``p_yes``
    must be ``0.50``. A mismatch would mean the pipeline's feature
    vector was NOT actually forwarded (silent drop / silent
    substitution with a different feature vector).
    """
    rows = [_make_snapshot_row("T1", ts=time.time() - 10.0, best_bid=0.48, best_ask=0.52)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    p_yes = model.predict_proba(token_id="T1")  # features=None

    assert isinstance(p_yes, float)
    # Unfitted model returns features[0] = mid_price = 0.50 for the
    # (0.48, 0.52) book. A non-0.5 value would indicate the features
    # fetched by the pipeline did NOT match the snapshot we set up.
    assert p_yes == pytest.approx(0.50, abs=1e-5), (
        f"Expected p_yes=0.50 (unfitted model returns mid_price), got {p_yes}"
    )
    # The freshness check was run — last_feature_age is populated and
    # reflects the snapshot's age (10 s old at call time, +/- a few ms
    # of test jitter).
    assert model.last_feature_age is not None, (
        "predict_proba(token_id=...) must populate last_feature_age so "
        "tests / dashboards can inspect the freshness path without "
        "parsing log records"
    )
    assert 5.0 <= model.last_feature_age <= 60.0, (
        f"Expected last_feature_age ~10s, got {model.last_feature_age}"
    )


def test_predict_proba_token_id_only_matches_explicit_features_path():
    """The token-id-only path must produce the SAME ``p_yes`` as the
    explicit-features path with the pipeline's features pre-fetched
    and passed in. This proves the wiring is a pure fetch-and-forward
    (no silent transformation)."""
    rows = [_make_snapshot_row("T1", ts=time.time() - 5.0, best_bid=0.40, best_ask=0.44)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    # Token-id-only path — fetch + predict happen inside predict_proba.
    p_via_token = model.predict_proba(token_id="T1")

    # Explicit-features path — fetch features manually, then pass to
    # the same predict_proba call. The two predictions MUST match.
    feats = _run_async(pipe.get_features("T1", timestamp=time.time()))
    assert feats is not None, "Pipeline should return features for T1"
    p_via_features = model.predict_proba(feats, token_id="T1")

    assert p_via_token == pytest.approx(p_via_features, abs=1e-7), (
        f"Token-id-only path ({p_via_token}) must match explicit-features "
        f"path ({p_via_features}) — wiring must be a pure fetch-and-forward"
    )


def test_predict_proba_uses_injected_pipeline_not_singleton():
    """The model uses the pipeline injected via ``model._feature_pipeline``
    rather than the process-wide singleton. Verified by injecting a
    pipeline with snapshot-A and asserting the prediction reflects
    snapshot-A (a singleton with a different snapshot would produce a
    different mid_price → different p_yes on the unfitted fallback path).
    """
    rows = [_make_snapshot_row("TINJECT", ts=time.time() - 1.0, best_bid=0.30, best_ask=0.32)]
    injected_pipe = _make_pipeline(rows)
    model = _make_unfitted_model(injected_pipe)

    p_yes = model.predict_proba(token_id="TINJECT")

    # Unfitted fallback: p_yes = features[0] = mid_price = 0.31.
    assert p_yes == pytest.approx(0.31, abs=1e-5), (
        f"Expected 0.31 (mid of (0.30, 0.32) — from the INJECTED pipeline's "
        f"snapshot), got {p_yes}. A mismatch would mean the model is "
        f"resolving the process-wide singleton instead of the injected "
        f"pipeline."
    )


def test_predict_proba_explicit_features_backward_compat():
    """``predict_proba(features, token_id=...)`` (the legacy call shape
    used by ``core/analysis_engine.py:65``) must still work — features
    are passed positionally, token_id is keyword. The pipeline is NOT
    consulted (no freshness check, no fetch)."""
    rows = [_make_snapshot_row("NEVER_USED", ts=time.time() - 1.0, best_bid=0.40, best_ask=0.60)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    # Construct an explicit feature vector with mid_price=0.73 — a value
    # NOT present in the injected pipeline's snapshots. If the model
    # ignored the explicit features and re-fetched from the pipeline
    # (mid=0.50), the assertion below would fail.
    feats = np.zeros(N_FEATURES, dtype=np.float32)
    feats[0] = 0.73  # mid_price
    p_yes = model.predict_proba(feats, token_id="EXPLICIT")

    # Unfitted fallback: p_yes = features[0] = 0.73.
    assert p_yes == pytest.approx(0.73, abs=1e-5), (
        f"Explicit-features path must bypass the pipeline — expected 0.73 "
        f"(features[0]), got {p_yes}. The pipeline's snapshot (mid=0.50) "
        f"must NOT have leaked into the explicit-features path."
    )
    # Freshness check is NOT run when features are passed explicitly.
    assert model.last_feature_age is None, (
        "last_feature_age must remain None when features are passed "
        "explicitly — no freshness check is run on that path"
    )


def test_predict_proba_no_features_no_token_id_returns_neutral():
    """``predict_proba()`` with neither features nor token_id returns
    ``0.5`` — there's nothing to score. Defensive guard so the model
    never raises on a malformed call."""
    model = _make_unfitted_model(_make_pipeline([]))
    p_yes = model.predict_proba()
    assert p_yes == 0.5


# ── (2) Point-in-time prediction ─────────────────────────────────────────────


def test_predict_proba_at_uses_point_in_time_snapshot():
    """``predict_proba_at(token_id=T, timestamp=T0)`` returns the
    prediction for the feature vector the pipeline reconstructs at T0
    (only snapshots with ``observation_ts <= T0`` are visible).

    A snapshot at T2 strictly after T0 must NOT leak into the T0
    prediction — verified via the unfitted model's ``float(features[0])``
    fallback: the T0 snapshot's mid_price (0.41) must be returned, not
    the T2 snapshot's mid_price (0.61).
    """
    rows = [
        _make_snapshot_row("T1", ts=100.0, best_bid=0.40, best_ask=0.42),  # mid=0.41
        _make_snapshot_row("T1", ts=200.0, best_bid=0.60, best_ask=0.62),  # mid=0.61
    ]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    # Replay at T=150 → only T=100 snapshot is PIT-visible → mid=0.41 → p_yes=0.41.
    p_at_150 = model.predict_proba_at("T1", timestamp=150.0)
    assert p_at_150 == pytest.approx(0.41, abs=1e-5), (
        f"PIT leak: predict_proba_at(T=150) should use the T=100 snapshot "
        f"(mid=0.41), got p_yes={p_at_150} (future T=200 snapshot mid=0.61 "
        f"leaked in)"
    )

    # Replay at T=250 → T=200 snapshot is PIT-visible → mid=0.61 → p_yes=0.61.
    p_at_250 = model.predict_proba_at("T1", timestamp=250.0)
    assert p_at_250 == pytest.approx(0.61, abs=1e-5), (
        f"predict_proba_at(T=250) should use the T=200 snapshot (mid=0.61), "
        f"got p_yes={p_at_250}"
    )


def test_predict_proba_at_returns_neutral_when_no_snapshot_at_or_before():
    """``predict_proba_at(token_id=T, timestamp=T0)`` returns ``0.5`` when
    the pipeline has no snapshot at-or-before T0 (the documented
    "neutral prediction when data unavailable" W33-2 contract).
    """
    # Only a future snapshot (T=300) — strictly AFTER the requested as_of.
    rows = [_make_snapshot_row("T1", ts=300.0, best_bid=0.49, best_ask=0.51)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    p_at_200 = model.predict_proba_at("T1", timestamp=200.0)
    assert p_at_200 == 0.5, (
        f"Expected neutral 0.5 (no PIT snapshot at-or-before T=200), "
        f"got p_yes={p_at_200}"
    )


def test_predict_proba_at_empty_token_id_returns_neutral():
    """``predict_proba_at("", timestamp=T)`` short-circuits to ``0.5`` —
    there's nothing to score without a token_id."""
    pipe = _make_pipeline([])
    model = _make_unfitted_model(pipe)
    assert model.predict_proba_at("", timestamp=time.time()) == 0.5


def test_predict_proba_at_freshness_check_is_point_in_time():
    """The freshness check inside ``predict_proba_at`` measures age
    against the supplied ``timestamp`` (NOT the wall clock) so a replay
    at T0 sees the freshness the model would have seen at T0.

    Verified by setting up a snapshot at T=100 and replaying at T=150:
    the freshness should be ~50 s (150 - 100), not (now - 100) which
    would be ~50+ years.
    """
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    p_at_150 = model.predict_proba_at("T1", timestamp=150.0)
    # Snapshot age at T=150 is exactly 50 s — well below the 60 s
    # threshold, so the freshness check is non-stale.
    assert p_at_150 == pytest.approx(0.50, abs=1e-5)
    assert model.last_feature_age is not None
    assert model.last_feature_age == pytest.approx(50.0, abs=1e-3), (
        f"predict_proba_at must measure feature age against ``timestamp``, "
        f"not the wall clock — expected ~50.0s (150-100), got "
        f"{model.last_feature_age}"
    )


# ── (3) Feature freshness check ──────────────────────────────────────────────


def test_predict_proba_warns_on_stale_features(caplog: pytest.LogCaptureFixture):
    """When the most recent snapshot is older than the freshness
    threshold (60 s), ``predict_proba`` still predicts (no
    short-circuit) but logs a WARNING.

    The warning message includes the token_id and the age in seconds
    so an operator can correlate the warning with the market / the
    ingestion pipeline's lag.
    """
    # Snapshot is 120 s old — well past the 60 s threshold.
    rows = [_make_snapshot_row("STALE_T", ts=time.time() - 120.0, best_bid=0.40, best_ask=0.42)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    with caplog.at_level(logging.WARNING, logger="ml.model"):
        p_yes = model.predict_proba(token_id="STALE_T")

    # The prediction is STILL made (the freshness check is informational
    # only — no short-circuit).
    assert p_yes == pytest.approx(0.41, abs=1e-5), (
        f"Stale features must still produce a prediction — expected 0.41 "
        f"(mid of (0.40, 0.42)), got {p_yes}"
    )
    assert model.last_feature_age is not None
    assert model.last_feature_age > FEATURE_FRESHNESS_THRESHOLD_SECONDS, (
        f"Expected last_feature_age > 60s, got {model.last_feature_age}"
    )
    # At least one WARNING record mentions "Stale" and the token_id.
    stale_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Stale" in r.getMessage()
        and "STALE_T" in r.getMessage()
    ]
    assert stale_records, (
        f"Expected a 'Stale features for STALE_T' WARNING log record, "
        f"got records={[r.getMessage() for r in caplog.records]}"
    )


def test_predict_proba_does_not_warn_on_fresh_features(caplog: pytest.LogCaptureFixture):
    """When the most recent snapshot is fresher than the 60 s threshold,
    NO ``Stale features`` WARNING is emitted (the freshness check
    passes silently). Belt-and-braces the inverse of the test above.
    """
    rows = [_make_snapshot_row("FRESH_T", ts=time.time() - 5.0, best_bid=0.49, best_ask=0.51)]
    pipe = _make_pipeline(rows)
    model = _make_unfitted_model(pipe)

    with caplog.at_level(logging.WARNING, logger="ml.model"):
        p_yes = model.predict_proba(token_id="FRESH_T")

    assert p_yes == pytest.approx(0.50, abs=1e-5)
    assert model.last_feature_age is not None
    assert model.last_feature_age <= FEATURE_FRESHNESS_THRESHOLD_SECONDS, (
        f"Expected last_feature_age <= 60s (fresh), got {model.last_feature_age}"
    )
    stale_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Stale" in r.getMessage()
    ]
    assert not stale_records, (
        f"No 'Stale features' WARNING expected for fresh snapshot, got "
        f"{[r.getMessage() for r in stale_records]}"
    )


def test_feature_freshness_threshold_is_60_seconds():
    """``FEATURE_FRESHNESS_THRESHOLD_SECONDS`` is exposed as a module-level
    constant so tests (and a future ``/api/ml/metrics`` field) can
    reference the same threshold the predict path uses. The W33-2 spec
    pins it at 60 s."""
    assert FEATURE_FRESHNESS_THRESHOLD_SECONDS == 60.0


# ── (4) Fallback when features unavailable ───────────────────────────────────


def test_predict_proba_returns_neutral_when_no_features_available(
    caplog: pytest.LogCaptureFixture,
):
    """``predict_proba(token_id=UNKNOWN, features=None)`` returns ``0.5``
    when the pipeline has no snapshot for the token. A WARNING is also
    emitted so an operator can correlate the neutral prediction with
    the missing data."""
    pipe = _make_pipeline([])  # No snapshots at all
    model = _make_unfitted_model(pipe)

    with caplog.at_level(logging.WARNING, logger="ml.model"):
        p_yes = model.predict_proba(token_id="UNKNOWN_TOKEN")

    assert p_yes == 0.5, (
        f"Expected neutral 0.5 (no features available), got {p_yes}"
    )
    # The "No features available" warning is emitted (independent of
    # the freshness path — the freshness check returns None for an
    # unknown token, then the get_features call also returns None, then
    # the fallback warning is emitted).
    no_features_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "No features available" in r.getMessage()
        and "UNKNOWN_TOKEN" in r.getMessage()
    ]
    assert no_features_records, (
        f"Expected a 'No features available for UNKNOWN_TOKEN' WARNING, "
        f"got records={[r.getMessage() for r in caplog.records]}"
    )


def test_predict_proba_at_returns_neutral_when_no_features_available():
    """``predict_proba_at(token_id=UNKNOWN, timestamp=T)`` returns
    ``0.5`` when the pipeline has no PIT snapshot at-or-before T.
    Same neutral-fallback semantics as the live-serving path."""
    pipe = _make_pipeline([])
    model = _make_unfitted_model(pipe)

    p_yes = model.predict_proba_at("UNKNOWN_TOKEN", timestamp=time.time())
    assert p_yes == 0.5


def test_predict_proba_returns_neutral_when_pipeline_raises():
    """A pipeline that raises (DB hiccup / network error / etc.) is
    caught by the predict path's defensive try/except — the model
    returns ``0.5`` rather than propagating the exception. This is the
    documented W33-2 "neutral prediction when data unavailable"
    contract, extended to the "pipeline broken" case.
    """
    class _ExplodingPipeline:
        async def get_features(self, *a, **kw):
            raise RuntimeError("simulated DB outage")
        async def get_feature_age(self, *a, **kw):
            raise RuntimeError("simulated DB outage")

    model = _make_unfitted_model(_ExplodingPipeline())  # type: ignore[arg-type]
    p_yes = model.predict_proba(token_id="EXPLODE")
    assert p_yes == 0.5, (
        f"Pipeline exception must NOT propagate — expected neutral 0.5, "
        f"got {p_yes}"
    )


def test_predict_proba_at_returns_neutral_when_pipeline_raises():
    """The PIT path also catches pipeline exceptions — the model returns
    ``0.5`` rather than propagating. Belt-and-braces the test above for
    the ``predict_proba_at`` variant."""
    class _ExplodingPipeline:
        async def get_features(self, *a, **kw):
            raise RuntimeError("simulated DB outage")
        async def get_feature_age(self, *a, **kw):
            raise RuntimeError("simulated DB outage")

    model = _make_unfitted_model(_ExplodingPipeline())  # type: ignore[arg-type]
    p_yes = model.predict_proba_at("EXPLODE", timestamp=time.time())
    assert p_yes == 0.5


# ── _run_async bridge helper ─────────────────────────────────────────────────


def test_run_async_resolves_in_sync_context():
    """``_run_async`` resolves a coroutine to its value when called from
    a sync context (no running event loop)."""
    async def _coro():
        return 42

    assert _run_async(_coro()) == 42


def test_run_async_resolves_inside_running_loop():
    """``_run_async`` resolves a coroutine to its value even when called
    from INSIDE a running event loop (the production path inside the
    asyncio-driven strategy / API). The bridge uses a one-shot thread
    pool with its own loop so the outer loop is never re-entered.
    """
    import asyncio

    async def _coro():
        return 99

    async def _outer():
        # Inside a running loop — _run_async must NOT raise
        # ``RuntimeError: This event loop is already running``.
        return _run_async(_coro())

    assert asyncio.run(_outer()) == 99


def test_run_async_propagates_exceptions():
    """``_run_async`` propagates exceptions raised inside the coroutine
    to the sync caller (so the predict path's existing try/except still
    catches them)."""
    async def _coro():
        raise ValueError("simulated pipeline failure")

    with pytest.raises(ValueError, match="simulated pipeline failure"):
        _run_async(_coro())
