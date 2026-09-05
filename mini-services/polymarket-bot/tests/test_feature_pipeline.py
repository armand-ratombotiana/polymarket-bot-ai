"""tests/test_feature_pipeline.py — W31-6 feature pipeline tests.

Five test areas required by the W31-6 task spec:

  (1) **Feature generation from market data** — the pipeline returns a
      38-dim ``float32`` numpy array matching
      ``ml.features.N_FEATURES`` for a valid snapshot, and ``None``
      when no snapshot is available at-or-before ``as_of``.

  (2) **Point-in-time correctness** — a snapshot at T2 is NOT visible
      to ``get_features(token_id, timestamp=T1 < T2)``: the resulting
      ``mid_price`` equals the T1 snapshot, not the T2 snapshot. The
      cyclical time features are derived from ``as_of`` rather than
      the wall clock.

  (3) **Feature versioning** — every contract in
      ``FEATURE_CONTRACTS`` carries a non-empty ``version`` string;
      ``FeaturePipeline.VERSION`` is exposed; the contract catalog
      matches ``ml.features.FEATURE_NAMES`` exactly (sanity-checked
      at import time in ``feature_contracts.py``).

  (4) **Provenance tracking** — ``get_features_with_provenance``
      returns a ``FeatureProvenance`` record carrying the snapshot
      timestamp that was used, the number of historical points that
      primed the rolling price-history deque, and the
      ``point_in_time`` / ``non_pit`` feature-name split. The
      provenance id is persisted to the feature store as the
      ``prediction_id`` on the per-feature ``feature_values`` rows.

  (5) **Wiring to ML model** — ``pipeline.predict_proba(token_id,
      ml_model=fake_model, ...)`` returns the same ``p_yes`` the
      fake model's ``predict`` returns for the pipeline's feature
      vector. The default ``ml_model`` (lazy singleton) is exercised
      via a smoke test that doesn't require a fitted model
      (``predict`` on an unfitted model returns ``features[0]``).

Module isolation
----------------
The pipeline reads its snapshot source from a pluggable
:class:`MockSnapshotSource` (in-memory list of snapshot dicts) so the
tests never touch the real ``db_manager`` SQLite fallback. The
``feature_store`` is a fresh :class:`FeatureStore(tmp_path / "fs.db")`
per-test so the persisted ``feature_values`` rows don't leak between
tests. ``ml.features._price_history`` (module-level deque) is cleared
in an autouse fixture so the rolling features don't accumulate across
tests.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). Mirrors the pattern in
# ``tests/test_ingestion_infra.py`` so the ``ingestion.*`` and
# ``core.timescale_db`` module-level singletons don't raise
# PermissionError on the read-only ``/app/data`` sandbox path.
_TMP_ROOT = Path("/tmp/pmbot_feature_pipeline_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-feature-pipeline",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# ── Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package. ──────────────────────────────────────────
# Same situation + fix as ``tests/test_ingestion_infra.py``: pytest's default
# ``prepend`` import mode inserts ``tests/`` at ``sys.path[0]`` during test
# collection, which lets the sibling ``tests/ingestion/`` package shadow our
# top-level ``polymarket-bot/ingestion/`` package. Without the ``remove``
# step below, the project root ends up behind ``tests/`` in sys.path, and
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

from core.data_store import OrderBook, PriceLevel  # noqa: E402
from ingestion.feature_contracts import (  # noqa: E402
    FEATURE_CONTRACTS,
    FeatureContract,
    non_pit_feature_names,
    pit_feature_names,
    register_all_contracts,
)
from ingestion.feature_pipeline import (  # noqa: E402
    FeaturePipeline,
    FeatureProvenance,
    get_feature_pipeline,
)
from ml import features as _feat_mod  # noqa: E402
from ml.features import FEATURE_NAMES, N_FEATURES  # noqa: E402
from ml.feature_store import FeatureStore  # noqa: E402

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────


class MockSnapshotSource:
    """In-memory snapshot source — most-recent-first (mirrors
    ``db_manager.get_snapshots`` return order)."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        # Sort most-recent-first so the mock faithfully mirrors the
        # real backend's contract (the pipeline relies on this order).
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
    extra_levels: int = 0,
    source: str = "clob_rest",
) -> dict:
    """Build a snapshot row mirroring ``db_manager.get_snapshots`` schema.

    ``extra_levels`` adds deeper ladder levels so the 5-level depth
    features have non-trivial values.
    """
    bids: list[dict] = [{"price": best_bid, "size": bid_size}]
    asks: list[dict] = [{"price": best_ask, "size": ask_size}]
    for i in range(extra_levels):
        bids.append({"price": round(best_bid - 0.01 * (i + 1), 4), "size": 50.0})
        asks.append({"price": round(best_ask + 0.01 * (i + 1), 4), "size": 50.0})
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


class _DummyMLModel:
    """Fake ML model — returns ``features[0]`` (mid_price) as ``p_yes``.

    Mirrors the ``MarketMLModel.predict`` signature so the pipeline's
    wiring test doesn't depend on a fitted model (the real model's
    ``predict`` returns ``features[0]`` when unfitted, which is the
    same behaviour this fake exhibits — the test asserts on that
    invariant).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[np.ndarray, str]] = []

    def predict(
        self, features: np.ndarray, token_id: str = ""
    ) -> tuple[float, float]:
        self.calls.append((features, token_id))
        return float(features[0]), abs(float(features[0]) - 0.5) * 2.0

    def predict_proba(
        self, features: np.ndarray, token_id: str = ""
    ) -> float:
        p, _ = self.predict(features, token_id=token_id)
        return p


# ── Autouse fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_price_history():
    """Clear ``ml.features._price_history`` before + after every test.

    The pipeline primes this deque from the snapshot window so the
    Hurst / momentum / rolling-vol features are point-in-time correct.
    Without a clear, state from one test would leak into the next and
    mask regressions (the same pattern as the autouse fixture in
    ``tests/test_features.py``).
    """
    _feat_mod._price_history.clear()
    yield
    _feat_mod._price_history.clear()


@pytest.fixture
def fresh_feature_store(tmp_path: Path) -> FeatureStore:
    """Fresh ``FeatureStore`` scoped to a ``tmp_path`` SQLite file.

    Each test gets a clean store so the persisted ``feature_values``
    rows don't leak between tests (mirrors the
    ``tests/test_feature_store.py`` hermeticity pattern).
    """
    return FeatureStore(db_path=tmp_path / "feature_pipeline_test.db")


# ── (1) Feature generation from market data ──────────────────────────────────


async def test_feature_generation_returns_38_dim_float32_vector():
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(
        db=MockSnapshotSource(rows), feature_store=FeatureStore(tmp_path := Path("/tmp") / "fs_smoke.db")
    )
    vec = await pipe.get_features("T1", timestamp=200.0)
    assert vec is not None, "Expected a feature vector for a valid snapshot"
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (N_FEATURES,), f"Expected ({N_FEATURES},), got {vec.shape}"
    assert vec.dtype == np.float32, f"Expected float32, got {vec.dtype}"


async def test_feature_generation_mid_price_matches_snapshot():
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.48, best_ask=0.52)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)
    vec = await pipe.get_features("T1", timestamp=200.0)
    assert vec is not None
    mid_idx = FEATURE_NAMES.index("mid_price")
    assert vec[mid_idx] == pytest.approx(0.5, abs=1e-5)


async def test_feature_generation_returns_none_when_no_snapshot():
    pipe = FeaturePipeline(db=MockSnapshotSource([]), feature_store=None)
    vec = await pipe.get_features("UNKNOWN", timestamp=200.0)
    assert vec is None


async def test_feature_generation_returns_none_when_only_future_snapshot_exists():
    # A snapshot strictly AFTER as_of must NOT be used (point-in-time).
    rows = [_make_snapshot_row("T1", ts=300.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)
    vec = await pipe.get_features("T1", timestamp=200.0)
    assert vec is None


async def test_feature_generation_uses_json_ladder_when_present():
    # Snapshot with a 5-level ladder — the cum_bid_depth_norm /
    # cum_ask_depth_norm / depth_imbalance_ratio features depend on
    # the ladder being parsed correctly.
    rows = [_make_snapshot_row(
        "T1", ts=100.0, best_bid=0.49, best_ask=0.51, extra_levels=4,
        bid_size=200.0, ask_size=100.0,
    )]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)
    vec = await pipe.get_features("T1", timestamp=200.0)
    assert vec is not None
    ofi_idx = FEATURE_NAMES.index("order_flow_imbalance")
    # OFI = (bid_sz - ask_sz) / (bid_sz + ask_sz) = (200-100)/(200+100) = 1/3
    assert vec[ofi_idx] == pytest.approx(1.0 / 3.0, abs=1e-5)


# ── (2) Point-in-time correctness ────────────────────────────────────────────


async def test_point_in_time_future_snapshot_not_used():
    """A snapshot at T2 must NOT be visible when as_of < T2."""
    rows = [
        _make_snapshot_row("T1", ts=100.0, best_bid=0.40, best_ask=0.42),  # mid=0.41
        _make_snapshot_row("T1", ts=200.0, best_bid=0.60, best_ask=0.62),  # mid=0.61
    ]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)

    # as_of = 150 → only the T1=100 snapshot is visible → mid should be 0.41
    vec = await pipe.get_features("T1", timestamp=150.0)
    assert vec is not None
    mid_idx = FEATURE_NAMES.index("mid_price")
    assert vec[mid_idx] == pytest.approx(0.41, abs=1e-5), (
        f"Point-in-time leak: as_of=150 should use T=100 snapshot (mid=0.41), "
        f"got mid={vec[mid_idx]} (future snapshot mid=0.61 leaked in)"
    )


async def test_point_in_time_latest_snapshot_at_or_before_as_of():
    """The pipeline picks the latest snapshot with ts <= as_of."""
    rows = [
        _make_snapshot_row("T1", ts=100.0, best_bid=0.40, best_ask=0.42),
        _make_snapshot_row("T1", ts=200.0, best_bid=0.50, best_ask=0.52),
        _make_snapshot_row("T1", ts=300.0, best_bid=0.60, best_ask=0.62),
    ]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)

    # as_of = 250 → latest snapshot <= as_of is T=200 (mid=0.51)
    vec = await pipe.get_features("T1", timestamp=250.0)
    assert vec is not None
    mid_idx = FEATURE_NAMES.index("mid_price")
    assert vec[mid_idx] == pytest.approx(0.51, abs=1e-5)


async def test_point_in_time_cyclical_time_features_derived_from_as_of():
    """``hour_sin`` / ``day_sin`` etc. must be derived from ``as_of``
    rather than the wall clock so a backtest replay at T0 produces
    the exact feature vector the model would have seen at T0."""
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)

    # as_of chosen so hour_frac = 0.5 (12:00 UTC) → hour_sin = sin(π) ≈ 0
    # 12:00:00 UTC on 2024-01-01 = epoch 1704110400
    import datetime as _dt
    as_of_dt = _dt.datetime(2024, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    as_of = as_of_dt.timestamp()

    vec = await pipe.get_features("T1", timestamp=as_of)
    assert vec is not None
    hour_sin_idx = FEATURE_NAMES.index("hour_sin")
    hour_cos_idx = FEATURE_NAMES.index("hour_cos")
    # hour_frac = 0.5 → hour_sin = sin(π) ≈ 1.2e-16, hour_cos = cos(π) = -1
    assert vec[hour_sin_idx] == pytest.approx(0.0, abs=1e-3)
    assert vec[hour_cos_idx] == pytest.approx(-1.0, abs=1e-3)


async def test_point_in_time_price_history_excludes_future():
    """The rolling price-history deque (Hurst / momentum / rolling-vol)
    must NOT include snapshots with ts > as_of."""
    # 4 historical mids: 0.4, 0.5, 0.55, 0.6 at T=100, 110, 120, 130
    rows = [
        _make_snapshot_row("T1", ts=100.0, best_bid=0.39, best_ask=0.41),
        _make_snapshot_row("T1", ts=110.0, best_bid=0.49, best_ask=0.51),
        _make_snapshot_row("T1", ts=120.0, best_bid=0.54, best_ask=0.56),
        _make_snapshot_row("T1", ts=130.0, best_bid=0.59, best_ask=0.61),
    ]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)

    # as_of = 125 → snapshot_row is the most-recent PIT snapshot (T=120,
    # mid=0.55); the rolling history deque is primed with the strictly-
    # older rows (T=100, T=110) and then ``extract_features`` appends
    # the snapshot_row's mid. The T=130 snapshot (mid=0.6) must NOT be
    # in the deque.
    vec, provenance = await pipe.get_features_with_provenance("T1", timestamp=125.0)
    assert vec is not None
    assert provenance is not None
    assert provenance.history_points_used == 2, (
        f"Expected 2 historical points (T=100,110 strictly before "
        f"snapshot_row at T=120) before as_of=125, "
        f"got {provenance.history_points_used}"
    )

    # Verify the price_history deque contents are point-in-time correct
    # (only T=100, 110, 120 mids; T=130 mid excluded).
    history = list(_feat_mod._price_history.get("T1", []))
    # ``extract_features`` appends the current mid to the deque, so the
    # deque has 2 historical mids + 1 current mid = 3 entries. The
    # current mid is the snapshot at T=120 (mid=0.55) — the latest
    # snapshot at-or-before as_of=125.
    assert len(history) == 3, f"Expected 3 entries (2 hist + 1 current), got {len(history)}"
    assert history == [0.4, 0.5, 0.55], (
        f"History should be [0.4, 0.5, 0.55] (point-in-time), got {history}"
    )
    assert 0.6 not in history, (
        f"Future mid 0.6 (T=130 > as_of=125) leaked into history: {history}"
    )


# ── (3) Feature versioning ───────────────────────────────────────────────────


def test_feature_contracts_catalog_matches_ml_feature_names():
    """The contract catalog MUST cover exactly the ML model's
    ``FEATURE_NAMES`` — a feature added to ``ml/features.py`` without
    a corresponding contract would silently ship without provenance."""
    assert set(FEATURE_CONTRACTS.keys()) == set(FEATURE_NAMES)
    assert len(FEATURE_CONTRACTS) == N_FEATURES == 38


def test_every_contract_has_nonempty_version():
    for name, contract in FEATURE_CONTRACTS.items():
        assert isinstance(contract, FeatureContract), f"{name} is not a FeatureContract"
        assert contract.version, f"{name} has empty version"
        assert contract.name == name, f"{name} contract name mismatch: {contract.name}"
        assert contract.type in ("numeric", "categorical", "boolean"), (
            f"{name} has invalid type: {contract.type}"
        )
        assert contract.source, f"{name} has empty source"
        assert contract.formula, f"{name} has empty formula"


def test_feature_pipeline_version_is_exposed():
    """``FeaturePipeline.VERSION`` is exposed + non-empty so callers can
    stamp it into their own provenance records."""
    assert FeaturePipeline.VERSION
    assert isinstance(FeaturePipeline.VERSION, str)


def test_pit_vs_non_pit_feature_split_is_explicit():
    """Every contract is explicitly tagged ``point_in_time=True`` or
    ``False`` so the provenance record can carry the exact split."""
    pit = set(pit_feature_names())
    non_pit = set(non_pit_feature_names())
    # The two sets partition the catalog — no overlap, no gaps.
    assert pit | non_pit == set(FEATURE_NAMES)
    assert pit & non_pit == set()
    # Sanity: cluster_correlation + fundamental_sentiment are explicitly
    # NOT point-in-time (they depend on live cache state).
    assert "cluster_correlation" in non_pit
    assert "fundamental_sentiment" in non_pit


def test_register_all_contracts_idempotent(fresh_feature_store: FeatureStore):
    """``register_all_contracts`` is idempotent — calling it twice
    doesn't double-insert (it's an upsert via ``register_feature``)."""
    n1 = register_all_contracts(fresh_feature_store, version="v1")
    n2 = register_all_contracts(fresh_feature_store, version="v1")
    assert n1 == n2 == N_FEATURES

    # Verify the feature_definitions table has exactly N_FEATURES rows
    # after a double-register (the upsert replaced, not appended).
    all_features = fresh_feature_store.get_all_features()
    assert len(all_features) == N_FEATURES


# ── (4) Provenance tracking ──────────────────────────────────────────────────


async def test_provenance_record_carries_snapshot_timestamp(fresh_feature_store: FeatureStore):
    rows = [_make_snapshot_row("T1", ts=123.0, best_bid=0.49, best_ask=0.51, source="clob_rest")]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=fresh_feature_store)

    vec, provenance = await pipe.get_features_with_provenance("T1", timestamp=200.0)
    assert vec is not None
    assert provenance is not None
    assert isinstance(provenance, FeatureProvenance)
    assert provenance.token_id == "T1"
    assert provenance.as_of == 200.0
    assert provenance.snapshot_timestamp == 123.0
    assert provenance.snapshot_source == "clob_rest"
    assert provenance.feature_version == FeaturePipeline.VERSION


async def test_provenance_records_history_points_used(fresh_feature_store: FeatureStore):
    rows = [
        _make_snapshot_row("T1", ts=100.0, best_bid=0.39, best_ask=0.41),
        _make_snapshot_row("T1", ts=110.0, best_bid=0.49, best_ask=0.51),
        _make_snapshot_row("T1", ts=120.0, best_bid=0.59, best_ask=0.61),
    ]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=fresh_feature_store)

    _, provenance = await pipe.get_features_with_provenance("T1", timestamp=125.0)
    assert provenance is not None
    assert provenance.history_points_used == 2  # T=100, 110 are strictly before 120


async def test_provenance_carries_pit_and_non_pit_feature_lists(fresh_feature_store: FeatureStore):
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=fresh_feature_store)

    _, provenance = await pipe.get_features_with_provenance("T1", timestamp=200.0)
    assert provenance is not None
    # The PIT list is non-empty (36 of 38 features are PIT).
    assert len(provenance.point_in_time_features) == 36
    # The non-PIT list is exactly the two live-state features.
    assert set(provenance.non_pit_features) == {"cluster_correlation", "fundamental_sentiment"}


async def test_provenance_id_persisted_to_feature_store(fresh_feature_store: FeatureStore):
    """The provenance id is the ``prediction_id`` on the persisted
    ``feature_values`` rows so an audit query can join a prediction
    back to its inputs."""
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=fresh_feature_store)

    vec, provenance = await pipe.get_features_with_provenance("T1", timestamp=200.0)
    assert vec is not None
    assert provenance is not None

    # Read the persisted rows directly from the SQLite store.
    import sqlite3
    with sqlite3.connect(fresh_feature_store._db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows_out = conn.execute(
            "SELECT feature_name, value, prediction_id FROM feature_values WHERE token_id = ?",
            ("T1",),
        ).fetchall()

    assert len(rows_out) == N_FEATURES, (
        f"Expected {N_FEATURES} persisted feature rows, got {len(rows_out)}"
    )
    # Every row carries the provenance_id as the prediction_id.
    prediction_ids = {r["prediction_id"] for r in rows_out}
    assert prediction_ids == {provenance.provenance_id}, (
        f"prediction_id mismatch: persisted={prediction_ids}, "
        f"provenance={provenance.provenance_id}"
    )
    # The mid_price row carries the expected value.
    mid_row = next(r for r in rows_out if r["feature_name"] == "mid_price")
    assert mid_row["value"] == pytest.approx(0.5, abs=1e-5)


async def test_provenance_to_dict_is_jsonable(fresh_feature_store: FeatureStore):
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=fresh_feature_store)

    _, provenance = await pipe.get_features_with_provenance("T1", timestamp=200.0)
    assert provenance is not None
    d = provenance.to_dict()
    # Round-trip through JSON to verify it's serialisable (operators
    # expose this via the HTTP API surface).
    round_tripped = json.loads(json.dumps(d))
    assert round_tripped["token_id"] == "T1"
    assert round_tripped["as_of"] == 200.0
    assert round_tripped["snapshot_timestamp"] == 100.0
    assert round_tripped["feature_version"] == FeaturePipeline.VERSION


# ── (5) Wiring to ML model ───────────────────────────────────────────────────


async def test_predict_proba_returns_model_output_for_pipeline_features():
    """``pipeline.predict_proba(token_id, ml_model=fake_model)`` returns
    the same ``p_yes`` the fake model's ``predict`` returns for the
    pipeline's feature vector. This proves the wiring is correct:
    pipeline features → model predict → returned p_yes."""
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)
    fake_model = _DummyMLModel()

    p_yes = await pipe.predict_proba("T1", ml_model=fake_model, timestamp=200.0)
    assert p_yes is not None
    assert len(fake_model.calls) == 1, "predict should be called exactly once"
    features_passed, token_passed = fake_model.calls[0]
    assert token_passed == "T1"
    # The fake returns ``features[0]`` (mid_price) as p_yes.
    assert p_yes == pytest.approx(float(features_passed[0]), abs=1e-7)
    # mid_price for (0.49, 0.51) book is 0.5.
    assert p_yes == pytest.approx(0.5, abs=1e-5)


async def test_predict_returns_p_yes_and_confidence_tuple():
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)
    fake_model = _DummyMLModel()

    result = await pipe.predict("T1", ml_model=fake_model, timestamp=200.0)
    assert result is not None
    p_yes, confidence = result
    assert p_yes == pytest.approx(0.5, abs=1e-5)
    # The fake's confidence is |p_yes - 0.5| * 2 = 0 for p_yes=0.5.
    assert confidence == pytest.approx(0.0, abs=1e-5)


async def test_predict_returns_none_when_no_features():
    pipe = FeaturePipeline(db=MockSnapshotSource([]), feature_store=None)
    result = await pipe.predict("UNKNOWN", ml_model=_DummyMLModel(), timestamp=200.0)
    assert result is None


async def test_predict_proba_smoke_uses_default_ml_model_when_none_passed():
    """When no ``ml_model=`` is injected, the pipeline falls back to the
    default ``ml.model.ml_model`` singleton. We exercise the wiring
    (not the prediction quality) — the test asserts only that
    ``predict_proba`` returns a float in [0, 1] (the model's clipped
    output range) rather than raising."""
    rows = [_make_snapshot_row("T1", ts=100.0, best_bid=0.49, best_ask=0.51)]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)

    # Patch ``ml.model.ml_model`` BEFORE the lazy resolve triggers.
    # The pipeline resolves the default on first ``predict`` call.
    from ml import model as _ml_model_mod
    fake_model = _DummyMLModel()
    original = _ml_model_mod.ml_model
    _ml_model_mod.ml_model = fake_model
    try:
        p_yes = await pipe.predict_proba("T1", timestamp=200.0)
    finally:
        _ml_model_mod.ml_model = original

    assert p_yes is not None
    assert 0.0 <= p_yes <= 1.0
    assert len(fake_model.calls) == 1


async def test_predict_proba_pit_wiring_returns_correct_mid_for_backtest():
    """End-to-end: backtest replay at T1 returns the model's prediction
    for the T1 feature vector (NOT T2)."""
    rows = [
        _make_snapshot_row("T1", ts=100.0, best_bid=0.40, best_ask=0.42),  # mid=0.41
        _make_snapshot_row("T1", ts=200.0, best_bid=0.60, best_ask=0.62),  # mid=0.61
    ]
    pipe = FeaturePipeline(db=MockSnapshotSource(rows), feature_store=None)
    fake_model = _DummyMLModel()

    # Replay at T=150 → only T=100 snapshot is visible → mid=0.41 → p_yes=0.41.
    p_yes = await pipe.predict_proba("T1", ml_model=fake_model, timestamp=150.0)
    assert p_yes is not None
    assert p_yes == pytest.approx(0.41, abs=1e-5), (
        f"Backtest PIT leak: as_of=150 should use T=100 snapshot (mid=0.41), "
        f"got p_yes={p_yes} (future snapshot mid=0.61 leaked in)"
    )

    # Replay at T=250 → T=200 snapshot is visible → mid=0.61 → p_yes=0.61.
    p_yes = await pipe.predict_proba("T1", ml_model=fake_model, timestamp=250.0)
    assert p_yes is not None
    assert p_yes == pytest.approx(0.61, abs=1e-5)


# ── Singleton ────────────────────────────────────────────────────────────────


def test_get_feature_pipeline_singleton_is_lazy_and_idempotent():
    """``get_feature_pipeline()`` returns the same singleton on every call."""
    # Reset the module-level singleton so this test is hermetic regardless
    # of any prior test that may have triggered the lazy construction.
    import ingestion.feature_pipeline as _fp_mod
    original = _fp_mod._feature_pipeline
    _fp_mod._feature_pipeline = None
    try:
        p1 = get_feature_pipeline()
        p2 = get_feature_pipeline()
        assert p1 is p2, "get_feature_pipeline() should return the same singleton"
        assert isinstance(p1, FeaturePipeline)
    finally:
        _fp_mod._feature_pipeline = original
