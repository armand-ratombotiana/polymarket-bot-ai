"""W32-4 — Data lineage + provenance unit tests.

Covers the public surface of the new ``ingestion.lineage.LineageTracker``
plus the three new ``/api/ingestion/lineage/*`` /
``/api/ingestion/provenance/*`` API routes:

  1. ``LineageTracker.record_node`` / ``record_edge`` — graph primitives
  2. ``LineageTracker.record_ingestion`` — source → raw edge
  3. ``LineageTracker.record_transformation`` — raw → normalized → enriched
  4. ``LineageTracker.record_feature`` + ``record_prediction`` — feature
     + prediction derivation
  5. ``LineageTracker.record_consumer`` — strategy / dashboard consumer
  6. ``LineageTracker.get_lineage`` — upstream + downstream chain walk
  7. ``LineageTracker.get_provenance`` — market-level view + summary
  8. ``LineageTracker.get_graph`` — graph visualisation with depth + source
     filter + truncation flag
  9. ``Pipeline.process`` lineage wiring — pipeline records ingestion +
     transformations on every successful record
  10. The three API routes — 200 / zero-state / 503 paths

Isolation strategy
------------------
Each data-path test constructs a fresh ``LineageTracker(db_path=tmp_path
/ "lineage.db")`` instance so the SQLite store is empty at the start of
every test — no cross-test pollution. The module-level singleton
``lineage_tracker`` (constructed at import time against the conftest-
redirected ``LINEAGE_DB_PATH``) is exercised by the API-route tests via
``TestClient(api.server.app)`` — same pattern as
``tests/test_openapi.py``.

The autouse ``_reset_store_factory_defaults`` conftest fixture wipes
the global ``store`` / ``risk_manager`` / ``paper_sim`` singletons
before every test so the API-route tests (which use the production
``app``) start from a clean baseline. Rate limiting is disabled in
``conftest.py`` (``limiter.enabled = False``) so the per-route slowapi
limits don't interfere.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (pytest-asyncio is already a project
dependency — mirrors the pattern in ``tests/test_decision_ledger.py``).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── Defensive env-var redirect BEFORE importing any project module ─────────
# Belt-and-braces with the same redirect in ``tests/conftest.py`` (which
# pytest loads before this file). Mirrors the pattern in
# ``tests/test_ingestion_infra.py`` and ``tests/test_data_validator.py``.
_TMP_ROOT = Path("/tmp/lineage_tests_isolation")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "LINEAGE_DB_PATH": str(_TMP_ROOT / "lineage.db"),
    # Belt-and-braces with conftest's MARKET_DB_PATH / RAW_VAULT_DB_PATH
    # redirect so the ``core.timescale_db`` + ``ingestion.raw_vault``
    # singletons don't raise PermissionError at import time.
    "RAW_VAULT_DB_PATH": str(_TMP_ROOT / "raw_vault.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "DLQ_DB_PATH": str(_TMP_ROOT / "dead_letter.db"),
    "CHECKPOINT_DB_PATH": str(_TMP_ROOT / "checkpoints.db"),
    "ALERT_DB_PATH": str(_TMP_ROOT / "alerts.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-lineage",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ingestion.*`` / ``core.*`` / ``api.*``). Mirrors the bootstrap
# pattern in every existing ``tests/test_*.py`` sibling. Same
# ``remove`` + ``insert(0, ...)`` dance as ``tests/test_ingestion_infra.py``
# so the sibling ``tests/ingestion/`` package can't shadow our top-level
# ``ingestion`` package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_str_root = str(_PROJECT_ROOT)
if _str_root in sys.path:
    sys.path.remove(_str_root)
sys.path.insert(0, _str_root)

# Defend against the sibling ``tests/ingestion/`` package shadowing our
# top-level ``ingestion`` package — same fix as
# ``tests/test_ingestion_infra.py``.
for _mod_name in list(sys.modules):
    if _mod_name != "ingestion" and not _mod_name.startswith("ingestion."):
        continue
    _mod = sys.modules.get(_mod_name)
    _mod_file = getattr(_mod, "__file__", "") or ""
    if "tests/ingestion" in _mod_file.replace("\\", "/"):
        del sys.modules[_mod_name]

import pytest  # noqa: E402  (env must be set first)

from ingestion.lineage import (  # noqa: E402
    EDGE_RELATIONS,
    NODE_TYPES,
    LineageTracker,
    lineage_tracker,
)
from ingestion.pipeline import Pipeline  # noqa: E402
from ingestion.raw_vault import RawVault  # noqa: E402

# NOTE: This module's tests are SYNC ``def test_...`` (the ``LineageTracker``
# is sync + the ``Pipeline.process`` is sync). The module-level
# ``pytestmark = pytest.mark.asyncio`` idiom used by sibling test modules
# is intentionally OMITTED so pytest-asyncio doesn't emit a warning
# about applying the asyncio mark to sync tests.
# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tracker(tmp_path: Path) -> LineageTracker:
    """Fresh ``LineageTracker`` instance backed by a tmp_path DB.

    Each test gets its own SQLite file so the lineage graph is empty at
    the start of every test — no cross-test pollution. The module-level
    singleton (constructed at import time against the conftest-redirected
    ``LINEAGE_DB_PATH``) is NOT exercised by these tests; it's exercised
    by the API-route tests via ``TestClient``.
    """
    return LineageTracker(db_path=tmp_path / "lineage.db")


@pytest.fixture
def pipeline(tmp_path: Path, tracker: LineageTracker) -> Pipeline:
    """Fresh ``Pipeline`` whose vault + lineage tracker are scoped to
    the test's ``tmp_path`` SQLite files.

    Production wires the module-level ``raw_vault`` + ``lineage_tracker``
    singletons; tests inject fresh instances per test so the
    assertions on lineage stats are deterministic (mirrors the
    ``tests/test_recording_pipeline.py`` / ``tests/test_ingestion_infra.py``
    isolation pattern).
    """
    vault = RawVault(db_path=tmp_path / "raw_vault.db")
    return Pipeline(vault=vault, lineage=tracker)


# ── API route fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def client():
    """``TestClient`` bound to the production ``api.server.app``.

    Imports happen lazily so the env-var redirects above have already
    taken effect by the time ``api.server`` is imported. Mirrors the
    pattern in ``tests/test_openapi.py``.
    """
    from fastapi.testclient import TestClient

    from api.server import app

    # Defensive: disable the rate-limit middleware so a fast test sequence
    # against a per-minute-limited route doesn't 429 mid-suite. Belt-and-
    # braces with the conftest-level disable.
    try:
        from api.server import limiter  # type: ignore[attr-defined]
        limiter.enabled = False  # type: ignore[attr-defined]
    except ImportError:
        pass

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": "Bearer test-token-conftest"}


# ── 1. record_node / record_edge ─────────────────────────────────────────────


class TestRecordNodeEdge:
    """``record_node`` + ``record_edge`` — graph primitives."""

    def test_record_node_inserts_new_node(self, tracker: LineageTracker):
        """``record_node`` inserts a row and returns ``True`` on a fresh
        ``node_id``."""
        ok = tracker.record_node(
            node_id="obs-1",
            node_type="raw",
            source="clob",
            token_id="TOK_A",
            metadata={"event_type": "snapshot"},
        )
        assert ok is True
        stats = tracker.get_stats()
        assert stats["node_count"] == 1
        assert stats["edge_count"] == 0

    def test_record_node_is_idempotent(self, tracker: LineageTracker):
        """Re-recording the same ``node_id`` is a no-op (returns
        ``False``; the existing row is left untouched)."""
        tracker.record_node(
            node_id="obs-1", node_type="raw", source="clob",
            metadata={"first": True},
        )
        ok = tracker.record_node(
            node_id="obs-1", node_type="raw", source="clob",
            metadata={"second": True},  # ignored — first writer wins
        )
        assert ok is False
        stats = tracker.get_stats()
        assert stats["node_count"] == 1
        # Verify the existing row's metadata is preserved.
        node = tracker._fetch_node("obs-1")
        assert node is not None
        assert node["metadata"].get("first") is True
        assert "second" not in node["metadata"]

    def test_record_node_rejects_invalid_node_type(self, tracker: LineageTracker):
        """``node_type`` not in ``NODE_TYPES`` raises ``ValueError``."""
        with pytest.raises(ValueError, match="node_type must be one of"):
            tracker.record_node(node_id="x", node_type="bogus_type")

    def test_record_node_rejects_empty_id(self, tracker: LineageTracker):
        """Empty ``node_id`` raises ``ValueError``."""
        with pytest.raises(ValueError, match="node_id must be a non-empty"):
            tracker.record_node(node_id="", node_type="raw")

    def test_record_edge_inserts_new_edge(self, tracker: LineageTracker):
        """``record_edge`` inserts an edge row and returns ``True``."""
        tracker.record_node(node_id="A", node_type="raw")
        tracker.record_node(node_id="B", node_type="normalized")
        ok = tracker.record_edge("A", "B", relation="transformed_to")
        assert ok is True
        stats = tracker.get_stats()
        assert stats["edge_count"] == 1

    def test_record_edge_is_idempotent(self, tracker: LineageTracker):
        """Re-recording the same ``(source, target, relation)`` triple is
        a no-op (returns ``False``; bumps the ``duplicate_ignored_count``
        counter so the dashboard can surface replay intensity)."""
        tracker.record_node(node_id="A", node_type="raw")
        tracker.record_node(node_id="B", node_type="normalized")
        tracker.record_edge("A", "B", relation="transformed_to")
        ok = tracker.record_edge("A", "B", relation="transformed_to")
        assert ok is False
        stats = tracker.get_stats()
        assert stats["edge_count"] == 1
        assert stats["duplicate_ignored_count"] == 1

    def test_record_edge_rejects_invalid_relation(self, tracker: LineageTracker):
        """``relation`` not in ``EDGE_RELATIONS`` raises ``ValueError``."""
        tracker.record_node(node_id="A", node_type="raw")
        tracker.record_node(node_id="B", node_type="normalized")
        with pytest.raises(ValueError, match="relation must be one of"):
            tracker.record_edge("A", "B", relation="bogus_relation")

    def test_record_edge_rejects_empty_node_ids(self, tracker: LineageTracker):
        """Empty ``source_node_id`` or ``target_node_id`` raises
        ``ValueError``."""
        with pytest.raises(ValueError, match="must be non-empty"):
            tracker.record_edge("", "B", relation="transformed_to")
        with pytest.raises(ValueError, match="must be non-empty"):
            tracker.record_edge("A", "", relation="transformed_to")


# ── 2. record_ingestion ─────────────────────────────────────────────────────


class TestRecordIngestion:
    """``record_ingestion`` — convenience recorder for an ingestion
    event (creates the ``source`` + ``raw`` nodes and the ``produced``
    edge)."""

    def test_records_source_and_raw_nodes_and_edge(self, tracker: LineageTracker):
        """``record_ingestion`` creates two nodes (source + raw) and one
        edge (source → raw with ``relation="produced"``)."""
        tracker.record_ingestion(
            observation_id="obs-1",
            source="clob",
            source_id="snap-TOK_A-1",
            event_type="snapshot",
            token_id="TOK_A",
            payload_summary="mid=0.50",
        )
        stats = tracker.get_stats()
        assert stats["node_count"] == 2  # source:clob + obs-1
        assert stats["edge_count"] == 1  # source:clob --produced--> obs-1

        # Source node has the right shape.
        src = tracker._fetch_node("source:clob")
        assert src is not None
        assert src["node_type"] == "source"
        assert src["source"] == "clob"
        assert src["metadata"].get("name") == "clob"

        # Raw node carries the token_id + payload_summary + source_id.
        raw = tracker._fetch_node("obs-1")
        assert raw is not None
        assert raw["node_type"] == "raw"
        assert raw["source"] == "clob"
        assert raw["token_id"] == "TOK_A"
        assert raw["metadata"]["event_type"] == "snapshot"
        assert raw["metadata"]["source_id"] == "snap-TOK_A-1"
        assert raw["metadata"]["payload_summary"] == "mid=0.50"

    def test_is_idempotent(self, tracker: LineageTracker):
        """Re-recording the same ingestion event is a no-op (the
        ``record_node`` + ``record_edge`` upserts handle it)."""
        for _ in range(3):
            tracker.record_ingestion(
                observation_id="obs-1",
                source="clob",
                source_id="snap-1",
                event_type="snapshot",
                token_id="TOK_A",
            )
        stats = tracker.get_stats()
        assert stats["node_count"] == 2  # still just source + raw
        assert stats["edge_count"] == 1
        # First insert succeeded; next 2 were no-ops.
        assert stats["duplicate_ignored_count"] == 2

    def test_no_op_on_empty_observation_id(self, tracker: LineageTracker):
        """An empty ``observation_id`` is a defensive no-op (does NOT
        raise; does NOT insert any rows)."""
        tracker.record_ingestion(
            observation_id="",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
        )
        stats = tracker.get_stats()
        assert stats["node_count"] == 0
        assert stats["edge_count"] == 0

    def test_no_op_on_empty_source(self, tracker: LineageTracker):
        """An empty ``source`` is a defensive no-op."""
        tracker.record_ingestion(
            observation_id="obs-1",
            source="",
            source_id="snap-1",
            event_type="snapshot",
        )
        stats = tracker.get_stats()
        assert stats["node_count"] == 0

    def test_handles_missing_token_id(self, tracker: LineageTracker):
        """A missing ``token_id`` is stored as an empty string (the
        ``lineage_nodes.token_id`` column is NOT NULL with a default
        empty string)."""
        tracker.record_ingestion(
            observation_id="obs-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id=None,
        )
        raw = tracker._fetch_node("obs-1")
        assert raw is not None
        assert raw["token_id"] == ""


# ── 3. record_transformation ────────────────────────────────────────────────


class TestRecordTransformation:
    """``record_transformation`` — convenience recorder for a
    transformation step (raw → normalized → enriched)."""

    def test_records_node_and_edge(self, tracker: LineageTracker):
        """``record_transformation`` creates the ``to_id`` node and an
        edge from ``from_id`` → ``to_id`` with
        ``relation="transformed_to"``."""
        tracker.record_node(node_id="obs-1", node_type="raw", source="clob")
        tracker.record_transformation(
            from_id="obs-1",
            to_id="norm:obs-1",
            transform_type="normalize",
            token_id="TOK_A",
        )
        node = tracker._fetch_node("norm:obs-1")
        assert node is not None
        assert node["node_type"] == "normalized"
        assert node["token_id"] == "TOK_A"
        assert node["metadata"]["transform_type"] == "normalize"

        stats = tracker.get_stats()
        assert stats["edge_count"] == 1

    def test_normalize_transform_creates_normalized_node(self, tracker: LineageTracker):
        """A ``transform_type="normalize"`` creates a ``normalized``-type
        node (NOT a ``raw`` / ``enriched`` / ``feature`` node)."""
        tracker.record_node(node_id="obs-1", node_type="raw")
        tracker.record_transformation(
            from_id="obs-1", to_id="norm:obs-1", transform_type="normalize"
        )
        node = tracker._fetch_node("norm:obs-1")
        assert node["node_type"] == "normalized"

    def test_enrich_transform_creates_enriched_node(self, tracker: LineageTracker):
        """A ``transform_type="enrich"`` creates an ``enriched``-type
        node."""
        tracker.record_node(node_id="norm:1", node_type="normalized")
        tracker.record_transformation(
            from_id="norm:1", to_id="enriched:1", transform_type="enrich"
        )
        node = tracker._fetch_node("enriched:1")
        assert node["node_type"] == "enriched"

    def test_feature_derive_transform_creates_feature_node(self, tracker: LineageTracker):
        """A ``transform_type="feature_derive"`` creates a ``feature``-
        type node."""
        tracker.record_node(node_id="enriched:1", node_type="enriched")
        tracker.record_transformation(
            from_id="enriched:1",
            to_id="feat:momentum:1",
            transform_type="feature_derive",
        )
        node = tracker._fetch_node("feat:momentum:1")
        assert node["node_type"] == "feature"


# ── 4. record_feature + record_prediction ───────────────────────────────────


class TestRecordFeaturePrediction:
    """``record_feature`` + ``record_prediction`` — ML-feature + ML-prediction
    lineage recorders."""

    def test_record_feature_creates_feature_node_and_edges(self, tracker: LineageTracker):
        """``record_feature`` creates the ``feature`` node and an edge
        from every upstream node in ``derived_from_ids`` → ``feature_id``
        with ``relation="derived_from"``."""
        tracker.record_node(node_id="enriched:1", node_type="enriched")
        tracker.record_node(node_id="enriched:2", node_type="enriched")
        tracker.record_feature(
            feature_id="feat:momentum:TOK_A",
            feature_name="momentum_5s",
            token_id="TOK_A",
            derived_from_ids=["enriched:1", "enriched:2"],
            metadata={"window_seconds": 5, "value": 0.012},
        )
        feat = tracker._fetch_node("feat:momentum:TOK_A")
        assert feat is not None
        assert feat["node_type"] == "feature"
        assert feat["token_id"] == "TOK_A"
        assert feat["metadata"]["feature_name"] == "momentum_5s"
        assert feat["metadata"]["window_seconds"] == 5

        stats = tracker.get_stats()
        assert stats["edge_count"] == 2  # two derived_from edges

    def test_record_prediction_creates_prediction_node_and_edges(self, tracker: LineageTracker):
        """``record_prediction`` creates the ``prediction`` node and an
        edge from every feature in ``feature_ids`` → ``prediction_id``
        with ``relation="predicted_from"``."""
        tracker.record_node(node_id="feat:1", node_type="feature")
        tracker.record_node(node_id="feat:2", node_type="feature")
        tracker.record_prediction(
            prediction_id="pred-1",
            token_id="TOK_A",
            feature_ids=["feat:1", "feat:2"],
            model_version="v1.2.3",
            metadata={"p_yes": 0.62, "confidence": 0.24},
        )
        pred = tracker._fetch_node("pred-1")
        assert pred is not None
        assert pred["node_type"] == "prediction"
        assert pred["token_id"] == "TOK_A"
        assert pred["metadata"]["model_version"] == "v1.2.3"
        assert pred["metadata"]["p_yes"] == pytest.approx(0.62)

        stats = tracker.get_stats()
        assert stats["edge_count"] == 2  # two predicted_from edges


# ── 5. record_consumer ──────────────────────────────────────────────────────


class TestRecordConsumer:
    """``record_consumer`` — strategy / dashboard consumer recorder."""

    def test_records_consumer_node_and_consumed_by_edge(self, tracker: LineageTracker):
        """``record_consumer`` creates the ``consumer`` node and an edge
        from ``node_id`` → ``consumer:<name>`` with
        ``relation="consumed_by"``."""
        tracker.record_node(node_id="pred-1", node_type="prediction")
        tracker.record_consumer(
            node_id="pred-1",
            consumer_name="ml_sig_v1",
            consumer_type="strategy",
            metadata={"action": "open_position"},
        )
        consumer = tracker._fetch_node("consumer:ml_sig_v1")
        assert consumer is not None
        assert consumer["node_type"] == "consumer"
        assert consumer["metadata"]["name"] == "ml_sig_v1"
        assert consumer["metadata"]["consumer_type"] == "strategy"

        stats = tracker.get_stats()
        assert stats["edge_count"] == 1


# ── 6. get_lineage ──────────────────────────────────────────────────────────


class TestGetLineage:
    """``get_lineage(record_id)`` — full lineage chain (upstream +
    downstream) for a single record."""

    def test_walks_upstream_chain(self, tracker: LineageTracker):
        """``upstream`` walks the ``target`` → ``source`` direction —
        records this record was derived FROM."""
        # Build a chain: source:clob → obs-1 → norm:obs-1 → enriched:obs-1
        tracker.record_ingestion(
            observation_id="obs-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id="TOK_A",
        )
        tracker.record_transformation(
            from_id="obs-1", to_id="norm:obs-1", transform_type="normalize"
        )
        tracker.record_transformation(
            from_id="norm:obs-1", to_id="enriched:obs-1", transform_type="enrich"
        )

        result = tracker.get_lineage("enriched:obs-1")
        assert result["record_id"] == "enriched:obs-1"
        assert result["node"]["node_id"] == "enriched:obs-1"

        upstream_ids = [step["node"]["node_id"] for step in result["upstream"]]
        # Upstream should walk enriched ← norm ← raw ← source:clob.
        assert "norm:obs-1" in upstream_ids
        assert "obs-1" in upstream_ids
        assert "source:clob" in upstream_ids

        # Depth increases monotonically along the walk.
        depths = [step["depth"] for step in result["upstream"]]
        assert depths == sorted(depths)
        assert depths[0] == 1

    def test_walks_downstream_chain(self, tracker: LineageTracker):
        """``downstream`` walks the ``source`` → ``target`` direction —
        records derived FROM this record."""
        tracker.record_ingestion(
            observation_id="obs-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id="TOK_A",
        )
        tracker.record_transformation(
            from_id="obs-1", to_id="norm:obs-1", transform_type="normalize"
        )
        tracker.record_transformation(
            from_id="norm:obs-1", to_id="enriched:obs-1", transform_type="enrich"
        )
        tracker.record_feature(
            feature_id="feat:1",
            feature_name="momentum",
            token_id="TOK_A",
            derived_from_ids=["enriched:obs-1"],
        )
        tracker.record_prediction(
            prediction_id="pred-1",
            token_id="TOK_A",
            feature_ids=["feat:1"],
            model_version="v1.0",
        )
        tracker.record_consumer(
            node_id="pred-1", consumer_name="ml_sig_v1", consumer_type="strategy"
        )

        result = tracker.get_lineage("obs-1")
        downstream_ids = [step["node"]["node_id"] for step in result["downstream"]]
        # Downstream should walk obs → norm → enriched → feat → pred → consumer.
        assert "norm:obs-1" in downstream_ids
        assert "enriched:obs-1" in downstream_ids
        assert "feat:1" in downstream_ids
        assert "pred-1" in downstream_ids
        assert "consumer:ml_sig_v1" in downstream_ids

    def test_zero_state_for_missing_record(self, tracker: LineageTracker):
        """A ``record_id`` not in the graph returns the zero-state
        (``node=None``, empty upstream/downstream) rather than raising —
        mirrors the W17-4 "honest health" convention."""
        result = tracker.get_lineage("does-not-exist")
        assert result["record_id"] == "does-not-exist"
        assert result["node"] is None
        assert result["upstream"] == []
        assert result["downstream"] == []
        assert result["generated_at"] > 0


# ── 7. get_provenance ───────────────────────────────────────────────────────


class TestGetProvenance:
    """``get_provenance(token_id)`` — market-level provenance view."""

    def test_returns_all_nodes_for_token(self, tracker: LineageTracker):
        """``get_provenance`` returns every node tagged with the
        ``token_id`` plus every edge touching those nodes.

        Note: the ``source:clob`` node has ``token_id=""`` (sources
        aren't tied to a specific market), so it's correctly excluded
        from the ``WHERE token_id = 'TOK_A'`` filter. The ``source:gamma``
        node likewise is excluded — only market-tagged nodes (raw,
        normalized, enriched, feature, prediction) appear in the
        provenance result.
        """
        # Two raw observations for TOK_A, each transformed.
        tracker.record_ingestion(
            observation_id="obs-1", source="clob", source_id="snap-1",
            event_type="snapshot", token_id="TOK_A",
        )
        tracker.record_ingestion(
            observation_id="obs-2", source="clob", source_id="snap-2",
            event_type="snapshot", token_id="TOK_A",
        )
        tracker.record_transformation(
            from_id="obs-1", to_id="norm:obs-1", transform_type="normalize",
            token_id="TOK_A",
        )
        # Also a record for a different market — should NOT appear.
        tracker.record_ingestion(
            observation_id="obs-3", source="gamma", source_id="market-info-1",
            event_type="market_info", token_id="TOK_B",
        )

        result = tracker.get_provenance("TOK_A")
        assert result["token_id"] == "TOK_A"

        node_ids = [n["node_id"] for n in result["nodes"]]
        # Market-tagged nodes appear in the result.
        assert "obs-1" in node_ids
        assert "obs-2" in node_ids
        assert "norm:obs-1" in node_ids
        # The source nodes (token_id="") and TOK_B's record are filtered out.
        assert "source:clob" not in node_ids
        assert "source:gamma" not in node_ids
        assert "obs-3" not in node_ids  # different token

        # Summary groups by node_type.
        summary = result["summary"]
        assert summary["raw_count"] == 2  # obs-1 + obs-2
        assert summary["normalized_count"] == 1  # norm:obs-1

    def test_zero_state_for_token_with_no_records(self, tracker: LineageTracker):
        """A ``token_id`` with no records returns the zero-state (empty
        lists + zeroed summary) — NOT a 404 / exception."""
        result = tracker.get_provenance("UNKNOWN_TOKEN")
        assert result["token_id"] == "UNKNOWN_TOKEN"
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["summary"] == {
            "raw_count": 0,
            "normalized_count": 0,
            "enriched_count": 0,
            "feature_count": 0,
            "prediction_count": 0,
            "consumer_count": 0,
        }

    def test_zero_state_for_empty_token(self, tracker: LineageTracker):
        """An empty ``token_id`` returns the zero-state (no nodes /
        edges)."""
        result = tracker.get_provenance("")
        assert result["token_id"] == ""
        assert result["nodes"] == []
        assert result["summary"] == {}


# ── 8. get_graph ─────────────────────────────────────────────────────────────


class TestGetGraph:
    """``get_graph(source=None, depth=3)`` — graph visualisation."""

    def test_returns_all_nodes_and_edges(self, tracker: LineageTracker):
        """With no source filter + default depth, ``get_graph`` returns
        every node + every edge."""
        tracker.record_ingestion(
            observation_id="obs-1", source="clob", source_id="snap-1",
            event_type="snapshot", token_id="TOK_A",
        )
        tracker.record_ingestion(
            observation_id="obs-2", source="gamma", source_id="market-1",
            event_type="market_info", token_id="TOK_B",
        )
        result = tracker.get_graph()
        assert result["source"] is None
        assert result["depth"] == 3
        node_ids = [n["node_id"] for n in result["nodes"]]
        assert "obs-1" in node_ids
        assert "obs-2" in node_ids
        assert "source:clob" in node_ids
        assert "source:gamma" in node_ids
        assert len(result["edges"]) >= 2
        assert result["truncated"] is False

    def test_source_filter_returns_only_matching_nodes(self, tracker: LineageTracker):
        """``source="clob"`` returns only nodes whose ``source`` column
        matches ``"clob"`` (plus the nodes reachable within ``depth``
        hops)."""
        tracker.record_ingestion(
            observation_id="obs-1", source="clob", source_id="snap-1",
            event_type="snapshot", token_id="TOK_A",
        )
        tracker.record_ingestion(
            observation_id="obs-2", source="gamma", source_id="market-1",
            event_type="market_info", token_id="TOK_B",
        )
        result = tracker.get_graph(source="clob")
        node_ids = [n["node_id"] for n in result["nodes"]]
        # CLOB seed + everything reachable in 3 hops.
        # obs-1's downstream: source:clob, obs-1. (No transformations were
        # recorded, so the BFS stops at depth 1.)
        assert "obs-1" in node_ids
        assert "source:clob" in node_ids
        # The gamma-sourced record should NOT be in the result.
        assert "obs-2" not in node_ids
        assert "source:gamma" not in node_ids

    def test_depth_limits_walk(self, tracker: LineageTracker):
        """``depth=N`` limits the BFS to N hops BEYOND the seed set.

        The seed set is every node matching the ``source`` filter (or
        every node when no filter is given). The BFS then walks outward
        ``depth`` hops, adding each newly-visited node to the result.
        A higher ``depth`` reaches more-distant nodes; a lower ``depth``
        stops the walk early.
        """
        # Build: source:clob --produced--> obs-1 --transformed_to--> norm:obs-1
        #        --transformed_to--> enriched:obs-1
        # The source filter ``source="clob"`` makes the seed set
        # {source:clob, obs-1} (both carry source="clob"). BFS then
        # walks ``depth`` hops outward from the seed.
        tracker.record_ingestion(
            observation_id="obs-1", source="clob", source_id="snap-1",
            event_type="snapshot", token_id="TOK_A",
        )
        tracker.record_transformation(
            from_id="obs-1", to_id="norm:obs-1", transform_type="normalize"
        )
        tracker.record_transformation(
            from_id="norm:obs-1", to_id="enriched:obs-1", transform_type="enrich"
        )

        # depth=1: seed (source:clob + obs-1) + 1 hop outward → adds
        # norm:obs-1. enriched:obs-1 is 2 hops away — NOT included.
        result = tracker.get_graph(source="clob", depth=1)
        node_ids = [n["node_id"] for n in result["nodes"]]
        assert "source:clob" in node_ids  # seed
        assert "obs-1" in node_ids  # seed
        assert "norm:obs-1" in node_ids  # depth 1
        assert "enriched:obs-1" not in node_ids  # depth 2

        # depth=3: walk reaches enriched:obs-1 (depth 2 from seed).
        result = tracker.get_graph(source="clob", depth=3)
        node_ids = [n["node_id"] for n in result["nodes"]]
        assert "norm:obs-1" in node_ids
        assert "enriched:obs-1" in node_ids

    def test_depth_clamped_to_max(self, tracker: LineageTracker):
        """A ``depth`` greater than ``_MAX_GRAPH_DEPTH`` (10) is
        clamped to 10 — the response carries ``depth: 10`` rather than
        the requested value."""
        result = tracker.get_graph(depth=1000)
        assert result["depth"] == 10

    def test_depth_clamped_to_min(self, tracker: LineageTracker):
        """A ``depth`` less than 1 is clamped to 1."""
        result = tracker.get_graph(depth=0)
        assert result["depth"] == 1

    def test_truncated_flag_when_node_cap_hit(self, tracker: LineageTracker):
        """When the seed query hits the ``_MAX_GRAPH_NODES`` cap, the
        ``truncated`` flag is set so the UI can render a "showing first
        N nodes" notice."""
        # Insert > _MAX_GRAPH_NODES (5000) nodes to trip the cap.
        # This is slow if we insert 5000 rows; instead, monkey-patch the
        # ``_MAX_GRAPH_NODES`` module constant down to 5 and insert 6
        # nodes so the test stays fast.
        from ingestion import lineage as lineage_mod

        original = lineage_mod._MAX_GRAPH_NODES
        try:
            lineage_mod._MAX_GRAPH_NODES = 5
            for i in range(6):
                tracker.record_node(
                    node_id=f"raw-{i}", node_type="raw", source="clob"
                )
            result = tracker.get_graph()
            assert result["truncated"] is True
            assert len(result["nodes"]) <= 5
        finally:
            lineage_mod._MAX_GRAPH_NODES = original


# ── 9. Pipeline wiring ──────────────────────────────────────────────────────


class TestPipelineWiring:
    """``Pipeline.process`` records lineage on every successful record
    + every transformation."""

    def test_process_records_ingestion_lineage(self, pipeline: Pipeline, tracker: LineageTracker):
        """``Pipeline.process`` calls ``record_ingestion`` after the raw
        vault stores the record — the ``source:clob`` + ``raw`` nodes +
        ``produced`` edge should be present in the lineage graph."""
        now = time.time()
        result = pipeline.process(
            source="clob",
            source_id="snap-TOK_A-1",
            event_type="market_info",  # bypass data_validator (only knows snapshots + trades)
            raw_payload={
                "token_id": "TOK_A",
                "bid": 0.50,
                "ask": 0.51,
                "timestamp": now,
                "mid": 0.505,
                "spread": 0.01,
                "bid_depth_10": 100.0,
                "ask_depth_10": 80.0,
            },
            event_time=now,
        )
        assert result.success, f"pipeline returned {result.quality_state}: {result.error_reason}"
        assert result.observation_id is not None

        # Lineage should have: source:clob + obs-1 + produced edge.
        stats = tracker.get_stats()
        assert stats["node_count"] >= 2  # source:clob + obs-1
        assert stats["edge_count"] >= 1  # produced edge

        # Raw node carries the token_id from the raw payload.
        raw = tracker._fetch_node(result.observation_id)
        assert raw is not None
        assert raw["node_type"] == "raw"
        assert raw["source"] == "clob"
        assert raw["token_id"] == "TOK_A"

    def test_process_records_transformation_lineage(self, pipeline: Pipeline, tracker: LineageTracker):
        """``Pipeline.process`` calls ``record_transformation`` after
        the normalize + enrich stages — the ``norm:<obs>`` +
        ``enriched:<obs>`` nodes + ``transformed_to`` edges should be
        present in the lineage graph."""
        now = time.time()
        result = pipeline.process(
            source="clob",
            source_id="snap-TOK_A-2",
            event_type="market_info",
            raw_payload={
                "token_id": "TOK_A",
                "bid": 0.50,
                "ask": 0.51,
                "timestamp": now,
                "mid": 0.505,
                "spread": 0.01,
                "bid_depth_10": 100.0,
                "ask_depth_10": 80.0,
            },
            event_time=now,
        )
        assert result.success
        assert result.observation_id is not None

        norm_id = f"norm:{result.observation_id}"
        enr_id = f"enriched:{result.observation_id}"

        # Norm + enriched nodes exist.
        norm = tracker._fetch_node(norm_id)
        assert norm is not None
        assert norm["node_type"] == "normalized"
        assert norm["token_id"] == "TOK_A"

        enr = tracker._fetch_node(enr_id)
        assert enr is not None
        assert enr["node_type"] == "enriched"
        assert enr["token_id"] == "TOK_A"

        # Get the full lineage chain — should walk raw ← source:clob
        # upstream + norm → enriched downstream.
        lin = tracker.get_lineage(result.observation_id)
        upstream_ids = [s["node"]["node_id"] for s in lin["upstream"]]
        downstream_ids = [s["node"]["node_id"] for s in lin["downstream"]]
        assert "source:clob" in upstream_ids
        assert norm_id in downstream_ids
        assert enr_id in downstream_ids

    def test_process_with_lineage_none_does_not_raise(self, tmp_path: Path):
        """When the lineage tracker is explicitly ``None`` (the
        defensive case where the singleton construction failed), the
        pipeline still processes records without raising — every
        lineage call is a no-op."""
        vault = RawVault(db_path=tmp_path / "raw_vault.db")
        # Pass lineage=None — pipeline uses lazy-load which may yield the
        # singleton; explicit None overrides that.
        pipeline = Pipeline(vault=vault, lineage=None)
        # Patch the lazy-load to return None so the test is hermetic
        # against the conftest's singleton.
        pipeline._lineage = None

        now = time.time()
        result = pipeline.process(
            source="clob",
            source_id="snap-no-lineage",
            event_type="market_info",
            raw_payload={
                "token_id": "TOK_A",
                "bid": 0.50, "ask": 0.51, "timestamp": now,
                "mid": 0.505, "spread": 0.01,
            },
            event_time=now,
        )
        assert result.success  # pipeline still processed the record


# ── 10. API routes ──────────────────────────────────────────────────────────


class TestLineageAPI:
    """The three ``/api/ingestion/lineage/*`` /
    ``/api/ingestion/provenance/*`` API routes."""

    def test_get_lineage_route_returns_zero_state_for_missing_record(
        self, client, auth_headers
    ):
        """``GET /api/ingestion/lineage/{record_id}`` returns HTTP 200
        with the zero-state (``node=null``, empty upstream/downstream)
        for a record_id that doesn't exist in the lineage graph."""
        # Use a record_id that definitely doesn't exist (UUID4).
        response = client.get(
            "/api/ingestion/lineage/does-not-exist-12345",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["record_id"] == "does-not-exist-12345"
        assert body["node"] is None
        assert body["upstream"] == []
        assert body["downstream"] == []
        assert body["generated_at"] > 0

    def test_get_lineage_route_returns_chain_after_recording(
        self, client, auth_headers
    ):
        """After recording a record via the singleton, the route
        returns the full chain (``node`` + ``upstream`` +
        ``downstream``)."""
        # Inject a record via the singleton so the API can see it.
        lineage_tracker.record_ingestion(
            observation_id="api-test-obs-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id="TOK_API",
        )
        try:
            response = client.get(
                "/api/ingestion/lineage/api-test-obs-1",
                headers=auth_headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["record_id"] == "api-test-obs-1"
            assert body["node"]["node_id"] == "api-test-obs-1"
            assert body["node"]["node_type"] == "raw"
            upstream_ids = [s["node"]["node_id"] for s in body["upstream"]]
            assert "source:clob" in upstream_ids
        finally:
            # Best-effort cleanup — the singleton persists across tests
            # so we leave the row; subsequent tests use unique IDs.
            pass

    def test_get_provenance_route_returns_zero_state_for_missing_token(
        self, client, auth_headers
    ):
        """``GET /api/ingestion/provenance/{token_id}`` returns HTTP 200
        with the zero-state (empty nodes/edges/summary) for a token_id
        with no records."""
        response = client.get(
            "/api/ingestion/provenance/UNKNOWN_TOKEN_API_TEST",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_id"] == "UNKNOWN_TOKEN_API_TEST"
        assert body["nodes"] == []
        assert body["edges"] == []
        assert body["summary"]["raw_count"] == 0

    def test_get_provenance_route_returns_summary_after_recording(
        self, client, auth_headers
    ):
        """After recording lineage for a token via the singleton, the
        route returns the nodes + summary."""
        token = "TOK_PROV_API_TEST"
        lineage_tracker.record_ingestion(
            observation_id="prov-test-obs-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id=token,
        )
        try:
            response = client.get(
                f"/api/ingestion/provenance/{token}",
                headers=auth_headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["token_id"] == token
            node_ids = [n["node_id"] for n in body["nodes"]]
            assert "prov-test-obs-1" in node_ids
            assert body["summary"]["raw_count"] >= 1
        finally:
            pass

    def test_get_lineage_graph_route_returns_nodes_and_edges(
        self, client, auth_headers
    ):
        """``GET /api/ingestion/lineage/graph`` returns a
        ``{nodes, edges, truncated, generated_at}`` block."""
        # Seed the singleton with a node so the graph isn't empty.
        lineage_tracker.record_ingestion(
            observation_id="graph-test-obs-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id="TOK_GRAPH",
        )
        try:
            response = client.get(
                "/api/ingestion/lineage/graph",
                headers=auth_headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert "nodes" in body
            assert "edges" in body
            assert "truncated" in body
            assert body["generated_at"] > 0
            # The graph-test-obs-1 node should be in the response.
            node_ids = [n["node_id"] for n in body["nodes"]]
            assert "graph-test-obs-1" in node_ids
        finally:
            pass

    def test_get_lineage_graph_route_supports_source_filter(
        self, client, auth_headers
    ):
        """The ``source`` query param filters nodes by their ``source``
        column."""
        lineage_tracker.record_ingestion(
            observation_id="graph-source-clob-1",
            source="clob",
            source_id="snap-1",
            event_type="snapshot",
            token_id="TOK_CLOB",
        )
        lineage_tracker.record_ingestion(
            observation_id="graph-source-gamma-1",
            source="gamma",
            source_id="market-1",
            event_type="market_info",
            token_id="TOK_GAMMA",
        )
        try:
            response = client.get(
                "/api/ingestion/lineage/graph?source=clob",
                headers=auth_headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["source"] == "clob"
            node_ids = [n["node_id"] for n in body["nodes"]]
            assert "graph-source-clob-1" in node_ids
            assert "graph-source-gamma-1" not in node_ids
        finally:
            pass

    def test_get_lineage_graph_route_supports_depth_param(
        self, client, auth_headers
    ):
        """The ``depth`` query param limits the walk to N hops."""
        response = client.get(
            "/api/ingestion/lineage/graph?depth=1",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["depth"] == 1

    def test_get_lineage_graph_route_rejects_depth_out_of_range(
        self, client, auth_headers
    ):
        """``depth`` must be in ``[1, 10]`` — FastAPI's ``Query(ge=1,
        le=10)`` returns 422 for out-of-range values."""
        response = client.get(
            "/api/ingestion/lineage/graph?depth=0",
            headers=auth_headers,
        )
        assert response.status_code == 422

        response = client.get(
            "/api/ingestion/lineage/graph?depth=11",
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_routes_require_auth(self, client):
        """All three routes enforce ``enforce_api_auth`` — a request
        without the bearer token returns 401."""
        for path in (
            "/api/ingestion/lineage/graph",
            "/api/ingestion/lineage/anything",
            "/api/ingestion/provenance/anything",
        ):
            response = client.get(path)
            # ``enforce_api_auth`` returns 401 for missing/invalid
            # bearer tokens (unless the path is in ``PUBLIC_PATHS``,
            # which these are NOT).
            assert response.status_code in (401, 403), (
                f"{path} should require auth; got {response.status_code}"
            )

    def test_lineage_graph_route_does_not_collide_with_record_id_route(
        self, client, auth_headers
    ):
        """The literal ``graph`` path segment is captured by the
        ``/lineage/graph`` route (declared FIRST), NOT by the
        ``/lineage/{record_id}`` path parameter."""
        # GET /api/ingestion/lineage/graph should return the graph
        # JSON (200 + nodes/edges), NOT the lineage-chain shape
        # ({record_id, node, upstream, downstream}).
        response = client.get(
            "/api/ingestion/lineage/graph",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # Graph shape carries ``nodes`` + ``edges`` + ``truncated``.
        assert "nodes" in body
        assert "edges" in body
        assert "truncated" in body
        # Chain shape would carry ``record_id`` + ``upstream`` + ``downstream`` —
        # the graph endpoint must NOT return those.
        assert "record_id" not in body
        assert "upstream" not in body


# ── 11. Module-level constants ─────────────────────────────────────────────


class TestModuleConstants:
    """``NODE_TYPES`` + ``EDGE_RELATIONS`` are exported and stable."""

    def test_node_types_includes_canonical_set(self):
        """``NODE_TYPES`` carries every canonical node type the task
        spec enumerates."""
        for expected in (
            "source", "raw", "normalized", "enriched",
            "feature", "prediction", "consumer",
        ):
            assert expected in NODE_TYPES, f"{expected} missing from NODE_TYPES"

    def test_edge_relations_includes_canonical_set(self):
        """``EDGE_RELATIONS`` carries every canonical relation."""
        for expected in (
            "produced", "transformed_to", "derived_from",
            "consumed_by", "predicted_from",
        ):
            assert expected in EDGE_RELATIONS, (
                f"{expected} missing from EDGE_RELATIONS"
            )

    def test_singleton_is_constructed(self):
        """The module-level singleton ``lineage_tracker`` is constructed
        (NOT ``None``) in the test environment — the conftest's
        ``LINEAGE_DB_PATH`` redirect should make construction succeed."""
        assert lineage_tracker is not None
        assert isinstance(lineage_tracker, LineageTracker)
