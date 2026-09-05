"""``ingestion`` package — unified raw data vault + pipeline + connectors
+ dead-letter queue + checkpoints + health monitor + backfill engine.

W31-1 — Raw data vault layer (``raw_vault``). Durable raw observation
store. Every record is preserved verbatim with provenance fields
(source / source_id / event_type / event_timestamp /
ingestion_timestamp / processing_timestamp / data_version /
validation_status / quality_score / error_reason). Backed by a
dedicated SQLite file (``RAW_VAULT_DB_PATH``) so it does NOT collide
with the legacy ``core/ingestion/raw_vault.py`` PG-only implementation.

W31-2 — Unified ingestion pipeline (``pipeline``). Routes every record
through the four layers: raw → normalized → enriched → feature-ready.
Records carry the full bitemporal + quality provenance trail.

W31-3 — Source connectors (``connectors``) and historical backfill
engine (``backfill``). Each connector (CLOB REST / Gamma REST /
WebSocket / NewsSentiment) wraps an existing ``core.*`` client,
forwards every fetched payload through the pipeline, and reports
health metrics (request count, success rate, latency, last error).
``backfill`` provides the CLI-driven historical rebuild path.

W31-4 — Dead-letter queue (``dead_letter``) + checkpoint manager
(``checkpoint``) + ingestion health monitor (``health``).

  * ``dead_letter``  — durable DLQ. Records that fail validation /
                       normalisation / storage are preserved with
                       the error reason and full original payload
                       so they can be retried later (no data lost).
  * ``checkpoint``   — per-source checkpoint manager. Tracks the
                       last successfully processed record for each
                       source (timestamp / sequence / offset-based)
                       so a restart resumes exactly where the prior
                       process left off — no data missed, no duplicates.
  * ``health``       — ingestion pipeline health monitor. Tracks
                       throughput / latency / failure rate / DLQ
                       depth per source and fires alerts when
                       thresholds are crossed.

The package lives at the top level of ``polymarket-bot/`` (NOT under
``core/``) so it can import from ``core.*`` without the legacy
``core/ingestion/`` namespace-package ambiguity.

Defensive import strategy
-------------------------
Each sibling module import is wrapped in a defensive try/except so
the package itself remains importable when:

  * a sibling wave is mid-landing (the file does not yet exist on
    disk);
  * a sibling module's transitive dependency fails to import in the
    current environment (the W31-1/2/3 modules transitively import
    ``core.timescale_db`` whose module-level singleton tries to mkdir
    ``/app/data`` — not writable in the test sandbox).

The W31-4 modules (``dead_letter`` / ``checkpoint`` / ``health``) are
pure SQLite and do NOT depend on ``core.timescale_db``, so they are
imported eagerly without a try/except guard. The other sibling
modules are wrapped individually so a single broken sibling doesn't
cascade into a package-wide import failure.
"""
from __future__ import annotations

# ── W31-4 — DLQ + checkpoint + health (always available) ───────────────────
# Imported eagerly (no try/except) so ``from ingestion import
# dead_letter_queue`` works without surprises. The constructors are
# import-safe (SQLite ``_init_db`` is wrapped in try/except so an
# unwritable default path doesn't crash the import).
from ingestion.dead_letter import (
    DLQ_DB_PATH,
    DeadLetterQueue,
    DeadLetterRecord,
    dead_letter_queue,
)
from ingestion.checkpoint import (
    CHECKPOINT_DB_PATH,
    Checkpoint,
    CheckpointManager,
    checkpoint_manager,
)
from ingestion.health import (
    ALERT_DEBOUNCE,
    DLQ_DEPTH_THRESHOLD,
    ERROR_RATE_THRESHOLD,
    LATENCY_THRESHOLD,
    NO_DATA_THRESHOLD,
    IngestionHealthMonitor,
    SourceHealth,
    ingestion_health_monitor,
)

# ── W31-1 — raw vault (defensive) ──────────────────────────────────────────
try:  # pragma: no cover — depends on core.timescale_db which may raise
    from ingestion.raw_vault import (
        DATA_VERSION_CURRENT,
        VALIDATION_STATUSES,
        RawRecord,
        RawVault,
        raw_vault,
    )
except Exception:  # noqa: BLE001 — sibling modules must never break the package
    RawRecord = None  # type: ignore[assignment,misc]
    RawVault = None  # type: ignore[assignment,misc]
    raw_vault = None  # type: ignore[assignment]
    DATA_VERSION_CURRENT = None  # type: ignore[assignment]
    VALIDATION_STATUSES = None  # type: ignore[assignment]

# ── W31-2 — pipeline (defensive) ──────────────────────────────────────────
try:  # pragma: no cover — depends on core.timescale_db which may raise
    from ingestion.pipeline import (
        STALE_REJECT_THRESHOLD_S,
        Pipeline,
        PipelineRecord,
        PipelineResult,
        pipeline,
    )
except Exception:  # noqa: BLE001 — sibling modules must never break the package
    Pipeline = None  # type: ignore[assignment,misc]
    PipelineRecord = None  # type: ignore[assignment,misc]
    PipelineResult = None  # type: ignore[assignment,misc]
    pipeline = None  # type: ignore[assignment]
    STALE_REJECT_THRESHOLD_S = None  # type: ignore[assignment]

# ── W31-3 — connectors (defensive) ─────────────────────────────────────────
try:  # pragma: no cover — depends on core.timescale_db which may raise
    from ingestion.connectors import (
        BaseConnector,
        ConnectorHealth,
        ConnectorRegistry,
        ClobRestConnector,
        GammaRestConnector,
        NewsSentimentConnector,
        WebSocketConnector,
        connector_registry,
    )
except Exception:  # noqa: BLE001 — sibling modules must never break the package
    BaseConnector = None  # type: ignore[assignment,misc]
    ConnectorHealth = None  # type: ignore[assignment,misc]
    ConnectorRegistry = None  # type: ignore[assignment,misc]
    ClobRestConnector = None  # type: ignore[assignment,misc]
    GammaRestConnector = None  # type: ignore[assignment,misc]
    NewsSentimentConnector = None  # type: ignore[assignment,misc]
    WebSocketConnector = None  # type: ignore[assignment,misc]
    connector_registry = None  # type: ignore[assignment]

# ── W31-3 — backfill engine (defensive) ────────────────────────────────────
try:  # pragma: no cover — depends on core.timescale_db which may raise
    from ingestion.backfill import (
        BackfillCheckpoint,
        BackfillEngine,
        BackfillStats,
        BackfillStore,
        BackfillType,
        RateLimiter,
        backfill_engine,
    )
except Exception:  # noqa: BLE001 — sibling modules must never break the package
    BackfillCheckpoint = None  # type: ignore[assignment,misc]
    BackfillEngine = None  # type: ignore[assignment,misc]
    BackfillStats = None  # type: ignore[assignment,misc]
    BackfillStore = None  # type: ignore[assignment,misc]
    BackfillType = None  # type: ignore[assignment,misc]
    RateLimiter = None  # type: ignore[assignment,misc]
    backfill_engine = None  # type: ignore[assignment]

# ── Feature contracts + feature pipeline (defensive) ──────────────────────
try:  # pragma: no cover — depends on core.* which may raise
    from ingestion.feature_contracts import (
        FEATURE_CONTRACTS,
        FeatureContract,
        non_pit_feature_names,
        pit_feature_names,
        register_all_contracts,
    )
except Exception:  # noqa: BLE001 — sibling modules must never break the package
    FeatureContract = None  # type: ignore[assignment,misc]
    FEATURE_CONTRACTS = None  # type: ignore[assignment]
    register_all_contracts = None  # type: ignore[assignment]
    pit_feature_names = None  # type: ignore[assignment]
    non_pit_feature_names = None  # type: ignore[assignment]

try:  # pragma: no cover — depends on core.* which may raise
    from ingestion.feature_pipeline import (
        FeaturePipeline,
        FeatureProvenance,
        SnapshotSource,
        get_feature_pipeline,
    )
except Exception:  # noqa: BLE001 — sibling modules must never break the package
    FeaturePipeline = None  # type: ignore[assignment,misc]
    FeatureProvenance = None  # type: ignore[assignment,misc]
    SnapshotSource = None  # type: ignore[assignment,misc]
    get_feature_pipeline = None  # type: ignore[assignment]


__all__ = [
    # W31-4 — DLQ + checkpoint + health (always available)
    "DeadLetterQueue",
    "DeadLetterRecord",
    "dead_letter_queue",
    "DLQ_DB_PATH",
    "Checkpoint",
    "CheckpointManager",
    "checkpoint_manager",
    "CHECKPOINT_DB_PATH",
    "IngestionHealthMonitor",
    "SourceHealth",
    "ingestion_health_monitor",
    "NO_DATA_THRESHOLD",
    "ERROR_RATE_THRESHOLD",
    "DLQ_DEPTH_THRESHOLD",
    "LATENCY_THRESHOLD",
    "ALERT_DEBOUNCE",
    # W31-1 — raw vault (defensive)
    "RawRecord",
    "RawVault",
    "raw_vault",
    "DATA_VERSION_CURRENT",
    "VALIDATION_STATUSES",
    # W31-2 — pipeline (defensive)
    "Pipeline",
    "PipelineRecord",
    "PipelineResult",
    "pipeline",
    "STALE_REJECT_THRESHOLD_S",
    # W31-3 — connectors + backfill (defensive)
    "BaseConnector",
    "ConnectorHealth",
    "ConnectorRegistry",
    "ClobRestConnector",
    "GammaRestConnector",
    "NewsSentimentConnector",
    "WebSocketConnector",
    "connector_registry",
    "BackfillCheckpoint",
    "BackfillEngine",
    "BackfillStats",
    "BackfillStore",
    "BackfillType",
    "RateLimiter",
    "backfill_engine",
    # Feature contracts + pipeline (defensive)
    "FeatureContract",
    "FEATURE_CONTRACTS",
    "register_all_contracts",
    "pit_feature_names",
    "non_pit_feature_names",
    "FeaturePipeline",
    "FeatureProvenance",
    "SnapshotSource",
    "get_feature_pipeline",
]
