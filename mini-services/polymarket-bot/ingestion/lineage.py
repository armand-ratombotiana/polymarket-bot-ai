"""Data lineage tracker — traces data from source to consumption.

For every record, tracks:
1. Source: Where it came from (API, WebSocket, backfill)
2. Transformations: What was applied (normalize, enrich, aggregate)
3. Consumers: Who uses it (ML model, strategy, dashboard)
4. Derivatives: What was derived from it (features, predictions)

This enables:
- "Where did this data come from?"
- "What depends on this data?"
- "What happens if this source is unavailable?"
- Full audit trail for every data point

W32-4 — Data lineage + provenance.

The lineage tracker is SQLite-backed (a dedicated db file at
``LINEAGE_DB_PATH``, defaulting to ``/app/data/lineage.db``) so the
lineage graph survives process restarts and can be queried out-of-band
by an operator / dashboard. Mirrors the persistence convention of the
sibling ``ingestion.raw_vault`` / ``ingestion.dead_letter`` /
``ingestion.checkpoint`` modules — a dedicated SQLite file separate
from every other store so an ingestion hiccup can never perturb the
lineage audit trail.

Graph model
-----------
Two tables form a directed acyclic graph (DAG):

  * ``lineage_nodes`` — every record / feature / prediction / consumer
    that has ever flowed through the pipeline. Each node has a
    ``node_id`` (caller-supplied — typically the vault's
    ``observation_id`` for raw records, a synthesised
    ``"norm:<obs_id>"`` for transformed records, a feature name for
    ML features, a prediction id for ML predictions, etc.), a
    ``node_type`` (``"source"`` / ``"raw"`` / ``"normalized"`` /
    ``"enriched"`` / ``"feature"`` / ``"prediction"`` / ``"consumer"``),
    and a free-form ``metadata`` JSON blob for caller-supplied
    context (e.g. the raw payload summary, the model version, the
    strategy name).

  * ``lineage_edges`` — every directed relationship between two nodes.
    Each edge carries a ``relation`` string
    (``"produced"`` / ``"derived_from"`` / ``"consumed_by"`` /
    ``"transformed_to"`` / ``"trained_on"`` / ``"predicted_from"``)
    so the graph can answer both "where did this come from?" (walk
    the ``source`` → ``target`` direction) and "what depends on
    this?" (walk the reverse direction).

The ``UNIQUE (source_node_id, target_node_id, relation)`` constraint
makes the graph idempotent — recording the same edge twice is a no-op
(an ``INSERT OR IGNORE`` is used so the duplicate doesn't raise).

Query API
---------
Three convenience query shapes back the API endpoints:

  * ``get_lineage(record_id)`` — returns the full lineage chain for a
    single record: the upstream chain (where did this come from,
    walking the ``target`` → ``source`` direction) and the downstream
    chain (what depends on this, walking the ``source`` → ``target``
    direction). Used by ``GET /api/ingestion/lineage/{record_id}``.

  * ``get_provenance(token_id)`` — returns the lineage for every
    record tagged with the given ``token_id`` (a Polymarket market
    token). This is the "market-level" view that lets an operator
    ask "what's the provenance of everything we know about market
    X?" — raw observations, transformations, features, predictions,
    and consumers, grouped by type. Used by
    ``GET /api/ingestion/provenance/{token_id}``.

  * ``get_graph(source=None, depth=3)`` — returns a JSON-serialisable
    ``{nodes: [...], edges: [...]}`` block for visualisation. Walks
    the graph from every node (optionally filtered by ``source``)
    up to ``depth`` hops so a UI can render the surrounding
    sub-graph without pulling the entire lineage table. Used by
    ``GET /api/ingestion/lineage/graph``.

Pipeline wiring
---------------
``Pipeline.process`` calls ``record_ingestion`` after the raw vault
stores a record, then ``record_transformation`` for the normalize +
enrich stages. ``Pipeline.__init__`` accepts an optional ``lineage``
parameter so a test can inject a tmp_path-scoped tracker; production
wires the module-level ``lineage_tracker`` singleton. Every lineage
recording call is best-effort — a SQLite write failure is logged at
WARNING level and swallowed so the pipeline never breaks because of
the lineage tracker (mirrors the raw_vault's ``record_observation``
best-effort contract).

Concurrency
-----------
The tracker is single-threaded by design — every call site is either
a sync caller (``Pipeline.process`` is sync) or an async caller that
offloads via ``asyncio.to_thread`` (the API routes that mutate the
tracker are async, but they only read; writes happen via the sync
``Pipeline`` path). SQLite's default ``BEGIN IMMEDIATE`` transaction
gives serialisability across threads, and the ``check_same_thread=False``
flag lets the tracker be shared between the asyncio loop and
``asyncio.to_thread`` offloads without a ``ProgrammingError``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Default DB path. The conftest redirects this in tests; production
# resolves it to ``/app/data/lineage.db`` (the docker-compose volume
# bind-mount makes ``/app/data`` writable in the prod container).
DEFAULT_DB_PATH = "/app/data/lineage.db"

# Bounded depth for the graph-walk query so a misbehaving caller can't
# OOM the bot by requesting ``depth=1000`` on a 1M-node graph. The
# hard ceiling is enforced in ``get_graph`` regardless of what the
# caller asks for.
_MAX_GRAPH_DEPTH = 10

# Bounded node count for the graph-walk query — caps the response size
# so a 100k-node lineage graph doesn't yield a 50 MB JSON payload. The
# cap is on the number of nodes returned, not the number of edges
# (edges are bounded transitively via the node cap).
_MAX_GRAPH_NODES = 5000

# Canonical node-type vocabulary. Kept as a frozenset for the
# ``record_node`` precondition check; the DB layer accepts any TEXT
# value so a future type (e.g. ``"label"``) doesn't require a
# migration. The set is documented so a downstream visualisation can
# switch on it without fearing churn.
NODE_TYPES = frozenset({
    "source",        # upstream connector (clob / gamma / ws / news / backfill)
    "raw",           # raw observation stored in the raw_vault
    "normalized",    # schema-coerced payload post-_default_normalizer
    "enriched",      # derived fields post-_default_enricher
    "feature",       # ML feature value derived from enriched payload
    "prediction",    # ML model prediction (token_id + p_yes + confidence)
    "consumer",      # downstream consumer (strategy / dashboard / alert)
    "label",         # ground-truth label (for ML training / backtest)
})

# Canonical edge-relation vocabulary. Same convention as NODE_TYPES —
# a frozenset for the precondition check, free-form TEXT in the DB.
EDGE_RELATIONS = frozenset({
    "produced",         # source → raw (a connector produced a raw record)
    "transformed_to",   # raw → normalized / normalized → enriched
    "derived_from",     # feature / prediction → upstream node(s)
    "consumed_by",      # any node → consumer (strategy / dashboard)
    "trained_on",       # model → feature / label
    "predicted_from",   # prediction → feature
})


# ── Dataclass ──────────────────────────────────────────────────────────────────


@dataclass
class LineageNode:
    """A single node in the lineage graph.

    Attributes:
        node_id: Caller-supplied unique identifier. Typically the
            vault's ``observation_id`` for raw records, a synthesised
            ``"norm:<obs_id>"`` for transformed records, a feature
            name (``"feat:momentum_5s:<token_id>"``) for ML features,
            a prediction id (``"pred:<uuid4>"``) for ML predictions.
            The caller is responsible for uniqueness; the DB layer
            enforces it via the PRIMARY KEY.
        node_type: One of ``NODE_TYPES``. Documented above.
        source: Originating source name (``"clob"`` / ``"gamma"`` /
            ``"websocket"`` / ``"news"`` / ``"backfill"`` / ``"ml"`` /
            ``"strategy"``). Empty string when not applicable (e.g.
            a normalised node's source is inherited from its raw
            parent — the caller can leave this empty and let the
            query API fill it in by walking the upstream chain).
        token_id: Polymarket market token id (when applicable). Indexed
            so the ``get_provenance(token_id)`` query is O(log n)
            instead of a full table scan.
        metadata: Free-form JSON-serialisable dict for caller-supplied
            context (e.g. ``{"payload_summary": "..."}``,
            ``{"model_version": "v1.2.3"}``,
            ``{"strategy": "ml_sig_v1"}``). Stored as canonical JSON.
        created_at: Unix timestamp when the node was first recorded.
    """

    node_id: str
    node_type: str
    source: str = ""
    token_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass
class LineageEdge:
    """A single directed edge in the lineage graph.

    Attributes:
        source_node_id: The upstream node's id (the producer).
        target_node_id: The downstream node's id (the consumer / derivative).
        relation: One of ``EDGE_RELATIONS``. Documented above.
        metadata: Free-form JSON-serialisable dict (e.g.
            ``{"transform_type": "normalize"}``,
            ``{"feature_names": ["mid", "spread"]}``).
        created_at: Unix timestamp when the edge was first recorded.
    """

    source_node_id: str
    target_node_id: str
    relation: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


# ── Tracker ────────────────────────────────────────────────────────────────────


class LineageTracker:
    """SQLite-backed lineage graph (DAG of nodes + edges).

    Construction is import-safe — the SQLite ``_init_db`` is wrapped in
    a try/except so an unwritable default path (``/app/data`` is
    read-only in the sandbox) doesn't crash the import. On failure the
    tracker falls back to ``/tmp/lineage.db`` (mirrors the
    ``raw_vault`` fallback convention) and logs a WARNING so the
    operator sees the redirect.

    The class is safe to instantiate without env vars / paths set —
    the constructor falls back to ``DEFAULT_DB_PATH`` and creates the
    parent directory if missing. ``/app/data`` is the prod default
    (writable in the docker-compose volume); tests redirect via the
    ``LINEAGE_DB_PATH`` env var (handled by ``tests/conftest.py``).
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        path_str = (
            db_path
            or os.environ.get("LINEAGE_DB_PATH", DEFAULT_DB_PATH)
        )
        self._db_path = Path(path_str)
        # ``check_same_thread=False`` so the tracker can be shared
        # between the asyncio event loop thread and ``asyncio.to_thread``
        # offloads. SQLite's default ``BEGIN IMMEDIATE`` transaction
        # gives serialisability across the GIL boundary; the ``_lock``
        # is for in-process serialisation on the (rare) multi-thread
        # call sites.
        self._lock = threading.Lock()
        # Counters — surfaced via ``get_stats`` for the operator
        # dashboard / observability layer.
        self._node_count: int = 0
        self._edge_count: int = 0
        self._duplicate_ignored_count: int = 0  # INSERT OR IGNORE hits
        self._init_db()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the ``lineage_nodes`` + ``lineage_edges`` tables if
        missing.

        Idempotent so repeated ``LineageTracker()`` constructions
        against the same db file are safe. Parent directory is
        auto-created so a fresh sandbox with no ``/app/data`` directory
        works (mirrors ``raw_vault._init_db``).
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Defensive: if the parent isn't writable, fall back to
            # ``/tmp/lineage.db`` so the bot still boots. A logged
            # warning surfaces the redirect; the operator can fix the
            # path env var. Mirrors the raw_vault fallback convention.
            logger.warning(
                "[lineage] Cannot create parent dir %s: %s — "
                "falling back to /tmp/lineage.db",
                self._db_path.parent,
                e,
            )
            self._db_path = Path("/tmp/lineage.db")

        try:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS lineage_nodes (
                        node_id     TEXT    PRIMARY KEY,
                        node_type   TEXT    NOT NULL,
                        source      TEXT    NOT NULL DEFAULT '',
                        token_id    TEXT    NOT NULL DEFAULT '',
                        metadata    TEXT    NOT NULL DEFAULT '{}',
                        created_at  REAL    NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_lineage_nodes_token
                    ON lineage_nodes (token_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_lineage_nodes_source
                    ON lineage_nodes (source)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_lineage_nodes_type
                    ON lineage_nodes (node_type)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS lineage_edges (
                        edge_id          TEXT    PRIMARY KEY,
                        source_node_id   TEXT    NOT NULL,
                        target_node_id   TEXT    NOT NULL,
                        relation         TEXT    NOT NULL,
                        metadata         TEXT    NOT NULL DEFAULT '{}',
                        created_at       REAL    NOT NULL
                    )
                """)
                # Idempotent edge insertion: the UNIQUE constraint
                # makes the same edge recorded twice a no-op (the
                # caller may re-record the same lineage on a replay).
                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_lineage_edges_unique
                    ON lineage_edges (source_node_id, target_node_id, relation)
                """)
                # Query indexes — mirror the ``get_lineage`` /
                # ``get_graph`` filter dimensions.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_lineage_edges_source
                    ON lineage_edges (source_node_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_lineage_edges_target
                    ON lineage_edges (target_node_id)
                """)
                conn.commit()
        except sqlite3.Error as e:
            # Defensive: if the schema init fails (corrupted DB /
            # locked schema / etc.), the tracker still functions —
            # every write call will best-effort log + swallow the
            # error. Logged at WARNING so the operator sees it.
            logger.warning(
                "[lineage] Schema init failed: %s — writes will be "
                "best-effort no-ops",
                e,
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection (the tracker doesn't pool — SQLite
        file open is cheap, and a per-call connection prevents the
        ``database is locked`` errors a long-lived connection would
        surface under contention).
        """
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=5.0,
            isolation_level="IMMEDIATE",
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ── Public API: node + edge recording ────────────────────────────────

    def record_node(
        self,
        node_id: str,
        node_type: str,
        source: str = "",
        token_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Record (or upsert) a single node.

        Returns ``True`` if the node was newly inserted, ``False`` if
        a node with the same ``node_id`` already existed (the existing
        row is left untouched — the caller's metadata / source /
        token_id are NOT merged in. This mirrors the convention used
        by the raw_vault's UNIQUE constraint: the first writer wins).

        Args:
            node_id: Caller-supplied unique identifier (see
                ``LineageNode`` docstring for the convention).
            node_type: One of ``NODE_TYPES`` (else ``ValueError``).
            source: Originating source name.
            token_id: Polymarket market token id (when applicable).
            metadata: Free-form JSON-serialisable dict.
        """
        if node_type not in NODE_TYPES:
            raise ValueError(
                f"node_type must be one of {sorted(NODE_TYPES)}, "
                f"got {node_type!r}"
            )
        if not node_id:
            raise ValueError("node_id must be a non-empty string")

        meta = _safe_json(metadata or {})
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO lineage_nodes (
                        node_id, node_type, source, token_id,
                        metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (node_id, node_type, source, token_id, meta, now),
                )
                conn.commit()
                if cur.rowcount == 0:
                    # Node already existed — idempotent re-record.
                    return False
        except sqlite3.Error as e:
            logger.warning(
                "[lineage] record_node failed for node_id=%s: %s",
                node_id,
                e,
            )
            return False
        with self._lock:
            self._node_count += 1
        return True

    def record_edge(
        self,
        source_node_id: str,
        target_node_id: str,
        relation: str = "derived_from",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Record (or upsert) a single edge.

        Returns ``True`` if the edge was newly inserted, ``False`` if
        an edge with the same ``(source_node_id, target_node_id,
        relation)`` already existed (UNIQUE constraint — idempotent
        re-record).

        Args:
            source_node_id: The upstream node's id (must already exist
                in ``lineage_nodes`` — recording an edge to a missing
                node is allowed for forward-references but is logged
                at DEBUG so a misbehaving caller can be traced).
            target_node_id: The downstream node's id.
            relation: One of ``EDGE_RELATIONS`` (else ``ValueError``).
            metadata: Free-form JSON-serialisable dict.
        """
        if relation not in EDGE_RELATIONS:
            raise ValueError(
                f"relation must be one of {sorted(EDGE_RELATIONS)}, "
                f"got {relation!r}"
            )
        if not source_node_id or not target_node_id:
            raise ValueError(
                "source_node_id and target_node_id must be non-empty"
            )

        meta = _safe_json(metadata or {})
        now = time.time()
        edge_id = str(uuid.uuid4())
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO lineage_edges (
                        edge_id, source_node_id, target_node_id,
                        relation, metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (edge_id, source_node_id, target_node_id, relation, meta, now),
                )
                conn.commit()
                if cur.rowcount == 0:
                    # Edge already existed — bump the
                    # duplicate_ignored counter so the dashboard can
                    # surface replay intensity.
                    with self._lock:
                        self._duplicate_ignored_count += 1
                    return False
        except sqlite3.Error as e:
            logger.warning(
                "[lineage] record_edge failed for %s --%s--> %s: %s",
                source_node_id,
                relation,
                target_node_id,
                e,
            )
            return False
        with self._lock:
            self._edge_count += 1
        return True

    # ── Convenience: domain-specific recorders ───────────────────────────

    def record_ingestion(
        self,
        *,
        observation_id: str,
        source: str,
        source_id: str,
        event_type: str,
        token_id: str | None = None,
        payload_summary: str | None = None,
    ) -> None:
        """Record the lineage for a single ingestion event.

        Creates TWO nodes (``source`` + ``raw``) and ONE edge
        (``produced``) so the graph captures "this connector produced
        this raw observation" — the foundation edge that lets the
        query API answer "where did this data come from?".

        Best-effort: any SQLite write failure is logged + swallowed
        so the pipeline never breaks because of the lineage tracker.
        Idempotent: re-recording the same ingestion is a no-op (the
        ``record_node`` / ``record_edge`` upserts handle it).

        Args:
            observation_id: The vault's UUID4 for the raw record.
            source: Originating source (``"clob"`` / ``"gamma"`` /
                ``"websocket"`` / ``"news"`` / ``"backfill"``).
            source_id: The source's own ID for the record (e.g. a
                ``trade_id`` for trades, ``condition_id`` for markets).
            event_type: ``"snapshot"`` / ``"trade"`` / ``"order_book"``
                / ``"market_info"`` / ``"news"``.
            token_id: Polymarket market token id (when applicable).
                Stored on the raw node so the ``get_provenance`` query
                can find every record for a market.
            payload_summary: Short human-readable summary of the raw
                payload (capped at 200 chars by the caller). Stored
                in the raw node's ``metadata`` for the dashboard.
        """
        if not observation_id or not source:
            return  # defensive: nothing to record
        source_node_id = f"source:{source}"
        meta: dict[str, Any] = {
            "source_id": source_id,
            "event_type": event_type,
        }
        if payload_summary:
            meta["payload_summary"] = payload_summary[:200]
        try:
            self.record_node(
                node_id=source_node_id,
                node_type="source",
                source=source,
                metadata={"name": source},
            )
            self.record_node(
                node_id=observation_id,
                node_type="raw",
                source=source,
                token_id=token_id or "",
                metadata=meta,
            )
            self.record_edge(
                source_node_id=source_node_id,
                target_node_id=observation_id,
                relation="produced",
                metadata={"event_type": event_type},
            )
        except Exception as e:  # noqa: BLE001 — best-effort lineage
            logger.warning(
                "[lineage] record_ingestion failed for obs=%s: %s",
                observation_id,
                e,
            )

    def record_transformation(
        self,
        *,
        from_id: str,
        to_id: str,
        transform_type: str,
        token_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record the lineage for a transformation step.

        Creates the ``to_id`` node (if missing) and an edge from
        ``from_id`` → ``to_id`` with ``relation="transformed_to"``.
        The ``to_id`` node's ``node_type`` is derived from
        ``transform_type`` (``"normalize"`` → ``"normalized"``,
        ``"enrich"`` → ``"enriched"``).

        Best-effort + idempotent — same contract as
        ``record_ingestion``.

        Args:
            from_id: The upstream node id (typically a raw
                ``observation_id`` for the normalize stage, a
                ``"norm:<obs_id>"`` for the enrich stage).
            to_id: The downstream node id (typically
                ``"norm:<obs_id>"`` for the normalize stage,
                ``"enriched:<obs_id>"`` for the enrich stage).
            transform_type: ``"normalize"`` / ``"enrich"`` /
                ``"aggregate"`` / ``"feature_derive"``. Used to derive
                the ``to_id`` node's ``node_type``.
            token_id: Polymarket market token id (when applicable).
            metadata: Free-form JSON-serialisable dict (e.g.
                ``{"transform_version": "1.0"}``).
        """
        if not from_id or not to_id:
            return  # defensive
        node_type = _transform_type_to_node_type(transform_type)
        meta = dict(metadata or {})
        meta["transform_type"] = transform_type
        try:
            self.record_node(
                node_id=to_id,
                node_type=node_type,
                token_id=token_id or "",
                metadata=meta,
            )
            self.record_edge(
                source_node_id=from_id,
                target_node_id=to_id,
                relation="transformed_to",
                metadata=meta,
            )
        except Exception as e:  # noqa: BLE001 — best-effort lineage
            logger.warning(
                "[lineage] record_transformation failed for %s → %s: %s",
                from_id,
                to_id,
                e,
            )

    def record_prediction(
        self,
        *,
        prediction_id: str,
        token_id: str,
        feature_ids: list[str],
        model_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record the lineage for an ML prediction.

        Creates the ``prediction`` node and an edge from every feature
        in ``feature_ids`` → ``prediction_id`` with
        ``relation="predicted_from"``. This is the edge that lets the
        query API answer "which features → which raw data" by walking
        ``prediction`` → ``feature`` → ``enriched`` → ``raw``.

        Best-effort + idempotent — same contract as
        ``record_ingestion``.

        Args:
            prediction_id: Caller-supplied unique id (e.g.
                ``"pred:<uuid4>"``).
            token_id: Polymarket market token id.
            feature_ids: List of feature node ids the prediction was
                derived from (e.g. ``["feat:momentum_5s:TOK_A",
                "feat:spread:TOK_A"]``). Each feature must already
                exist in ``lineage_nodes`` (the ML feature store is
                responsible for recording feature nodes when it
                derives them — see ``record_feature`` below).
            model_version: The model version that produced the
                prediction (stored in the prediction node's
                ``metadata``).
            metadata: Free-form JSON-serialisable dict (e.g.
                ``{"p_yes": 0.62, "confidence": 0.24}``).
        """
        if not prediction_id:
            return  # defensive
        meta = dict(metadata or {})
        meta["model_version"] = model_version
        try:
            self.record_node(
                node_id=prediction_id,
                node_type="prediction",
                token_id=token_id,
                metadata=meta,
            )
            for feature_id in feature_ids:
                self.record_edge(
                    source_node_id=feature_id,
                    target_node_id=prediction_id,
                    relation="predicted_from",
                    metadata={"model_version": model_version},
                )
        except Exception as e:  # noqa: BLE001 — best-effort lineage
            logger.warning(
                "[lineage] record_prediction failed for pred=%s: %s",
                prediction_id,
                e,
            )

    def record_feature(
        self,
        *,
        feature_id: str,
        feature_name: str,
        token_id: str,
        derived_from_ids: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record the lineage for an ML feature.

        Creates the ``feature`` node and an edge from every upstream
        node in ``derived_from_ids`` → ``feature_id`` with
        ``relation="derived_from"``. The upstream nodes are typically
        enriched / normalized records the feature was computed from.

        Best-effort + idempotent — same contract as
        ``record_ingestion``.

        Args:
            feature_id: Caller-supplied unique id (e.g.
                ``"feat:momentum_5s:TOK_A"``).
            feature_name: Human-readable feature name (e.g.
                ``"momentum_5s"``).
            token_id: Polymarket market token id.
            derived_from_ids: List of upstream node ids the feature
                was computed from (e.g. ``["enriched:<obs_id_1>",
                "enriched:<obs_id_2>"]`` for a rolling-window feature).
            metadata: Free-form JSON-serialisable dict (e.g.
                ``{"window_seconds": 5, "value": 0.012}``).
        """
        if not feature_id:
            return  # defensive
        meta = dict(metadata or {})
        meta["feature_name"] = feature_name
        try:
            self.record_node(
                node_id=feature_id,
                node_type="feature",
                token_id=token_id,
                metadata=meta,
            )
            for upstream_id in derived_from_ids:
                self.record_edge(
                    source_node_id=upstream_id,
                    target_node_id=feature_id,
                    relation="derived_from",
                    metadata={"feature_name": feature_name},
                )
        except Exception as e:  # noqa: BLE001 — best-effort lineage
            logger.warning(
                "[lineage] record_feature failed for feat=%s: %s",
                feature_id,
                e,
            )

    def record_consumer(
        self,
        *,
        node_id: str,
        consumer_name: str,
        consumer_type: str = "strategy",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record that a consumer (strategy / dashboard / alert) used
        a node.

        Creates a ``consumer`` node for ``consumer_name`` (idempotent)
        and an edge from ``node_id`` → ``consumer:<consumer_name>`` with
        ``relation="consumed_by"``. This is the edge that lets the
        query API answer "what depends on this data?" by walking the
        ``source`` → ``target`` direction.

        Best-effort + idempotent — same contract as
        ``record_ingestion``.

        Args:
            node_id: The upstream node id (the data being consumed).
            consumer_name: The consumer's name (e.g.
                ``"ml_sig_v1"`` / ``"IngestionHealthPanel"`` /
                ``"alert_engine"``).
            consumer_type: ``"strategy"`` / ``"dashboard"`` /
                ``"alert"`` / ``"backtest"``. Stored in the consumer
                node's ``metadata``.
            metadata: Free-form JSON-serialisable dict.
        """
        if not node_id or not consumer_name:
            return  # defensive
        consumer_node_id = f"consumer:{consumer_name}"
        meta = dict(metadata or {})
        meta["consumer_type"] = consumer_type
        try:
            self.record_node(
                node_id=consumer_node_id,
                node_type="consumer",
                metadata={"name": consumer_name, "consumer_type": consumer_type},
            )
            self.record_edge(
                source_node_id=node_id,
                target_node_id=consumer_node_id,
                relation="consumed_by",
                metadata=meta,
            )
        except Exception as e:  # noqa: BLE001 — best-effort lineage
            logger.warning(
                "[lineage] record_consumer failed for node=%s consumer=%s: %s",
                node_id,
                consumer_name,
                e,
            )

    # ── Public API: query ───────────────────────────────────────────────

    def get_lineage(self, record_id: str) -> dict[str, Any]:
        """Get the full lineage chain for a record.

        Returns::

            {
              "record_id": str,
              "node": dict | None,         # the record's own node row
              "upstream": [dict, ...],     # walking source → target backwards
              "downstream": [dict, ...],  # walking source → target forwards
              "generated_at": float,
            }

        ``upstream`` walks the ``target`` → ``source`` direction
        (i.e. the records this record was derived FROM). ``downstream``
        walks the ``source`` → ``target`` direction (i.e. the records
        derived FROM this record). Both walks are bounded at
        ``_MAX_GRAPH_DEPTH`` hops so a 1M-node graph doesn't OOM the
        bot. Edges are returned alongside the nodes so a UI can render
        the connecting relations.

        ``record_id`` not in the graph → returns the zero-state
        (``node=None``, ``upstream=[]``, ``downstream=[]``) rather
        than raising — mirrors the W17-4 "honest health" convention.
        """
        now = time.time()
        node = self._fetch_node(record_id)
        if node is None:
            return {
                "record_id": record_id,
                "node": None,
                "upstream": [],
                "downstream": [],
                "generated_at": now,
            }
        upstream = self._walk(record_id, direction="upstream")
        downstream = self._walk(record_id, direction="downstream")
        return {
            "record_id": record_id,
            "node": node,
            "upstream": upstream,
            "downstream": downstream,
            "generated_at": now,
        }

    def get_provenance(self, token_id: str) -> dict[str, Any]:
        """Get provenance for all data related to a market.

        Returns every node tagged with ``token_id`` (raw observations,
        normalized records, enriched records, features, predictions)
        and every edge touching those nodes. This is the "market-level"
        view that lets an operator ask "what's the provenance of
        everything we know about market X?".

        Returns::

            {
              "token_id": str,
              "nodes": [dict, ...],
              "edges": [dict, ...],
              "summary": {
                "raw_count": int,
                "normalized_count": int,
                "enriched_count": int,
                "feature_count": int,
                "prediction_count": int,
                "consumer_count": int,
              },
              "generated_at": float,
            }

        ``token_id`` with no records → returns the zero-state (empty
        lists, zeroed summary) rather than raising.
        """
        now = time.time()
        if not token_id:
            return {
                "token_id": token_id,
                "nodes": [],
                "edges": [],
                "summary": {},
                "generated_at": now,
            }
        try:
            with self._connect() as conn:
                node_rows = conn.execute(
                    """
                    SELECT * FROM lineage_nodes
                    WHERE token_id = ?
                    ORDER BY created_at ASC
                    """,
                    (token_id,),
                ).fetchall()
                node_ids = [r["node_id"] for r in node_rows]
                if not node_ids:
                    return {
                        "token_id": token_id,
                        "nodes": [],
                        "edges": [],
                        "summary": self._empty_summary(),
                        "generated_at": now,
                    }
                placeholders = ",".join("?" for _ in node_ids)
                edge_rows = conn.execute(
                    f"""
                    SELECT * FROM lineage_edges
                    WHERE source_node_id IN ({placeholders})
                       OR target_node_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    (*node_ids, *node_ids),
                ).fetchall()
        except sqlite3.Error as e:
            logger.warning(
                "[lineage] get_provenance failed for token=%s: %s",
                token_id,
                e,
            )
            return {
                "token_id": token_id,
                "nodes": [],
                "edges": [],
                "summary": self._empty_summary(),
                "generated_at": now,
            }
        nodes = [_node_row_to_dict(r) for r in node_rows]
        edges = [_edge_row_to_dict(r) for r in edge_rows]
        summary = self._summarize_nodes(nodes)
        return {
            "token_id": token_id,
            "nodes": nodes,
            "edges": edges,
            "summary": summary,
            "generated_at": now,
        }

    def get_graph(
        self,
        source: str | None = None,
        depth: int = 3,
    ) -> dict[str, Any]:
        """Get the lineage graph (for visualisation).

        Returns a JSON-serialisable ``{nodes, edges, generated_at}``
        block. Walks the graph from every node (optionally filtered by
        ``source`` — e.g. ``source="clob"`` returns only nodes
        originating from the CLOB connector) up to ``depth`` hops so a
        UI can render the surrounding sub-graph without pulling the
        entire lineage table.

        The walk is bounded by ``_MAX_GRAPH_NODES`` so a 100k-node
        lineage graph doesn't yield a 50 MB JSON payload. When the cap
        is hit, the response carries a ``truncated: True`` flag so the
        UI can render a "showing first N nodes" notice.

        Args:
            source: Optional source filter — when set, only nodes whose
                ``source`` column matches are returned (and the walk
                starts from those nodes). ``None`` returns every node
                (subject to the cap).
            depth: Maximum number of hops to walk from each starting
                node. Clamped to ``[1, _MAX_GRAPH_DEPTH]``.
        """
        now = time.time()
        hops = max(1, min(int(depth), _MAX_GRAPH_DEPTH))

        # Seed the BFS with every node matching the source filter (or
        # every node when source is None). The seed query is bounded
        # by the cap so a 1M-node graph doesn't OOM at the seed step.
        try:
            with self._connect() as conn:
                if source is not None:
                    seed_rows = conn.execute(
                        """
                        SELECT node_id FROM lineage_nodes
                        WHERE source = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (source, _MAX_GRAPH_NODES),
                    ).fetchall()
                else:
                    seed_rows = conn.execute(
                        """
                        SELECT node_id FROM lineage_nodes
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (_MAX_GRAPH_NODES,),
                    ).fetchall()
                seed_ids = [r["node_id"] for r in seed_rows]
                # BFS over the edge graph, collecting node ids up to
                # ``hops`` levels deep. The visited set deduplicates
                # so a node reached via two paths is only included
                # once.
                visited: set[str] = set(seed_ids)
                frontier: list[str] = list(seed_ids)
                touched_edges: set[str] = set()
                for _level in range(hops):
                    if not frontier or len(visited) >= _MAX_GRAPH_NODES:
                        break
                    next_frontier: list[str] = []
                    # Batch-fetch every edge touching the current
                    # frontier (both directions) so the BFS doesn't
                    # round-trip per node.
                    if frontier:
                        placeholders = ",".join("?" for _ in frontier)
                        edge_rows = conn.execute(
                            f"""
                            SELECT * FROM lineage_edges
                            WHERE source_node_id IN ({placeholders})
                               OR target_node_id IN ({placeholders})
                            """,
                            (*frontier, *frontier),
                        ).fetchall()
                        for er in edge_rows:
                            touched_edges.add(er["edge_id"])
                            for nid in (er["source_node_id"], er["target_node_id"]):
                                if nid not in visited and len(visited) < _MAX_GRAPH_NODES:
                                    visited.add(nid)
                                    next_frontier.append(nid)
                    frontier = next_frontier
                # Materialise the node + edge rows for the visited set
                # + touched edges.
                if visited:
                    placeholders = ",".join("?" for _ in visited)
                    node_rows = conn.execute(
                        f"""
                        SELECT * FROM lineage_nodes
                        WHERE node_id IN ({placeholders})
                        ORDER BY created_at ASC
                        """,
                        tuple(visited),
                    ).fetchall()
                else:
                    node_rows = []
                if touched_edges:
                    placeholders = ",".join("?" for _ in touched_edges)
                    edge_rows = conn.execute(
                        f"""
                        SELECT * FROM lineage_edges
                        WHERE edge_id IN ({placeholders})
                        ORDER BY created_at ASC
                        """,
                        tuple(touched_edges),
                    ).fetchall()
                else:
                    edge_rows = []
                # Determine if the seed query was truncated by the
                # _MAX_GRAPH_NODES cap. ``seed_rows`` was fetched with
                # LIMIT _MAX_GRAPH_NODES; if the count matches the
                # cap, there may be more rows in the DB.
                truncated = len(seed_rows) >= _MAX_GRAPH_NODES
        except sqlite3.Error as e:
            logger.warning(
                "[lineage] get_graph failed for source=%s: %s",
                source,
                e,
            )
            return {
                "source": source,
                "depth": hops,
                "nodes": [],
                "edges": [],
                "truncated": False,
                "generated_at": now,
            }
        nodes = [_node_row_to_dict(r) for r in node_rows]
        edges = [_edge_row_to_dict(r) for r in edge_rows]
        return {
            "source": source,
            "depth": hops,
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "generated_at": now,
        }

    # ── Public API: stats / reset ───────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Return live tracker counters (JSON-serialisable).

        Returned keys: ``node_count`` (cumulative successful inserts
        in this process), ``edge_count`` (cumulative successful edge
        inserts), ``duplicate_ignored_count`` (UNIQUE-constraint hits
        on idempotent re-records), ``db_path`` (the resolved SQLite
        path — useful for debugging a misconfigured env var).
        """
        with self._lock:
            return {
                "node_count": self._node_count,
                "edge_count": self._edge_count,
                "duplicate_ignored_count": self._duplicate_ignored_count,
                "db_path": str(self._db_path),
            }

    def reset_stats(self) -> None:
        """Zero the in-memory counters (test-only — does NOT truncate
        the DB). Mirrors the W24-4 ``data_validator._seen_ids.clear()``
        pattern in ``tests/conftest.py``'s autouse fixture.
        """
        with self._lock:
            self._node_count = 0
            self._edge_count = 0
            self._duplicate_ignored_count = 0

    def close(self) -> None:
        """No-op (the tracker opens a per-call connection, so there's
        no long-lived connection to close). Kept for API symmetry with
        ``RawVault.close`` / ``ClobClient.close`` / ``GammaClient.close``
        so a caller that loops over every connector + storage in a
        shutdown list doesn't crash on the tracker.
        """
        return None

    # ── Helpers ─────────────────────────────────────────────────────────

    def _fetch_node(self, node_id: str) -> dict[str, Any] | None:
        """Fetch a single node row by id. Returns ``None`` if missing."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM lineage_nodes WHERE node_id = ?",
                    (node_id,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning(
                "[lineage] _fetch_node failed for %s: %s", node_id, e
            )
            return None
        if row is None:
            return None
        return _node_row_to_dict(row)

    def _walk(
        self,
        node_id: str,
        direction: str,
        max_depth: int = _MAX_GRAPH_DEPTH,
    ) -> list[dict[str, Any]]:
        """BFS over the edge graph from ``node_id``.

        ``direction="upstream"`` walks the ``target`` → ``source``
        direction (records this node was derived FROM).
        ``direction="downstream"`` walks the ``source`` → ``target``
        direction (records derived FROM this node).

        Returns a list of ``{"node": <node_row>, "edge": <edge_row>,
        "depth": int}`` dicts ordered by depth then by created_at.
        """
        if direction not in ("upstream", "downstream"):
            raise ValueError(f"direction must be 'upstream' or 'downstream'")
        try:
            with self._connect() as conn:
                visited: set[str] = {node_id}
                results: list[dict[str, Any]] = []
                frontier: list[str] = [node_id]
                for level in range(1, max_depth + 1):
                    if not frontier:
                        break
                    next_frontier: list[str] = []
                    # Batch-fetch every edge touching the current
                    # frontier in the requested direction.
                    placeholders = ",".join("?" for _ in frontier)
                    if direction == "upstream":
                        # walk target → source: find edges where
                        # target_node_id is in the frontier, and
                        # follow to source_node_id.
                        sql = (
                            f"SELECT * FROM lineage_edges "
                            f"WHERE target_node_id IN ({placeholders})"
                        )
                        params: tuple[Any, ...] = tuple(frontier)
                        next_attr = "source_node_id"
                    else:
                        # walk source → target: find edges where
                        # source_node_id is in the frontier, and
                        # follow to target_node_id.
                        sql = (
                            f"SELECT * FROM lineage_edges "
                            f"WHERE source_node_id IN ({placeholders})"
                        )
                        params = tuple(frontier)
                        next_attr = "target_node_id"
                    edge_rows = conn.execute(sql, params).fetchall()
                    for er in edge_rows:
                        next_id = er[next_attr]
                        # Fetch the next node's row so the result
                        # carries the full node metadata.
                        node_row = conn.execute(
                            "SELECT * FROM lineage_nodes WHERE node_id = ?",
                            (next_id,),
                        ).fetchone()
                        if node_row is None:
                            # Forward-reference edge to a missing
                            # node — skip (shouldn't happen, but
                            # defensive).
                            continue
                        results.append({
                            "node": _node_row_to_dict(node_row),
                            "edge": _edge_row_to_dict(er),
                            "depth": level,
                        })
                        if next_id not in visited:
                            visited.add(next_id)
                            next_frontier.append(next_id)
                    frontier = next_frontier
        except sqlite3.Error as e:
            logger.warning(
                "[lineage] _walk failed for %s (direction=%s): %s",
                node_id,
                direction,
                e,
            )
            return []
        return results

    @staticmethod
    def _empty_summary() -> dict[str, int]:
        return {
            "raw_count": 0,
            "normalized_count": 0,
            "enriched_count": 0,
            "feature_count": 0,
            "prediction_count": 0,
            "consumer_count": 0,
        }

    @staticmethod
    def _summarize_nodes(nodes: list[dict[str, Any]]) -> dict[str, int]:
        """Group node rows by ``node_type`` and count each."""
        summary = LineageTracker._empty_summary()
        for n in nodes:
            nt = n.get("node_type", "")
            if nt == "raw":
                summary["raw_count"] += 1
            elif nt == "normalized":
                summary["normalized_count"] += 1
            elif nt == "enriched":
                summary["enriched_count"] += 1
            elif nt == "feature":
                summary["feature_count"] += 1
            elif nt == "prediction":
                summary["prediction_count"] += 1
            elif nt == "consumer":
                summary["consumer_count"] += 1
        return summary


# ── Module-level helpers ──────────────────────────────────────────────────────


def _safe_json(obj: Any) -> str:
    """Serialise ``obj`` to canonical JSON. Falls back to ``{}`` on
    failure (a non-JSON-serialisable ``metadata`` dict should never
    crash the tracker — better to store an empty dict than to drop
    the node entirely).
    """
    try:
        return json.dumps(obj, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def _node_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``lineage_nodes`` row to a dict with ``metadata``
    parsed back from JSON.
    """
    d = dict(row)
    raw = d.get("metadata")
    if isinstance(raw, str):
        try:
            d["metadata"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Keep the raw string — better to surface the unparsed
            # bytes than to drop the metadata from the query result.
            pass
    return d


def _edge_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``lineage_edges`` row to a dict with ``metadata``
    parsed back from JSON.
    """
    d = dict(row)
    raw = d.get("metadata")
    if isinstance(raw, str):
        try:
            d["metadata"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def _transform_type_to_node_type(transform_type: str) -> str:
    """Map a ``transform_type`` string to the canonical ``node_type``.

    ``"normalize"`` → ``"normalized"``
    ``"enrich"`` → ``"enriched"``
    ``"feature_derive"`` → ``"feature"``
    anything else → ``"enriched"`` (defensive default — better to
    record the edge with a slightly-wrong node type than to drop it).
    """
    mapping = {
        "normalize": "normalized",
        "enrich": "enriched",
        "aggregate": "enriched",  # aggregates roll into the enriched layer
        "feature_derive": "feature",
    }
    return mapping.get(transform_type, "enriched")


# ── Module-level singleton ────────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion module
# (``raw_vault``, ``dead_letter_queue``, ``checkpoint_manager`` …).
# Importers grab it at module-import time; the constructor opens the DB
# and runs migrations — the I/O is bounded (a single SQLite file + a
# few ``CREATE TABLE IF NOT EXISTS`` queries) so the import-time cost
# is negligible.
#
# Defensive: if construction fails (e.g. the SQLite file is on a
# read-only path AND the /tmp fallback also fails — extremely rare),
# ``lineage_tracker`` is set to ``None`` so the pipeline's
# best-effort wiring no-ops rather than crashing every ``process``
# call.
try:
    lineage_tracker = LineageTracker()
except Exception as e:  # noqa: BLE001 — defensive: must never block import
    logger.warning(
        "[lineage] singleton construction failed: %s — lineage recording "
        "will be a no-op",
        e,
    )
    lineage_tracker = None  # type: ignore[assignment]


__all__ = [
    "LineageNode",
    "LineageEdge",
    "LineageTracker",
    "lineage_tracker",
    "NODE_TYPES",
    "EDGE_RELATIONS",
    "DEFAULT_DB_PATH",
]
