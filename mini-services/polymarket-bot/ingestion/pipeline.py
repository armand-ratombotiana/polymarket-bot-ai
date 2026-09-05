"""Unified ingestion pipeline — all data flows through this.

Architecture:
  Source → Connector → Validator → Normalizer → Router → Storage

Layers:
  1. Raw layer: Exact source payload (raw_vault)
  2. Normalized layer: Standardized schema
  3. Enriched layer: Computed fields (mid, spread, depth)
  4. Feature-ready layer: ML features derived from enriched

Each record gets:
  - event_time, ingestion_time, processing_time
  - source, source_id
  - quality_state, error_reason

Pipeline stages
---------------
The pipeline routes a single record through four logical stages, each
adding a layer of derived data:

  * **Raw**        — the exact bytes the source sent. Stored in the
    ``raw_vault`` so a future replay can re-run every downstream stage
    from scratch. The raw layer is the ONLY layer that's always
    present (every other layer is computed FROM it).

  * **Normalized** — schema-coerced: timestamps are floats, prices /
    sizes are floats, sides are upper-cased. Mirrors the W24-4
    ``DataValidator`` output shape (the pipeline delegates snapshot /
    trade validation to the existing ``data_validator`` singleton so
    the validation rules don't drift between the two paths).

  * **Enriched**   — derived fields (``mid``, ``spread``,
    ``depth_10``). Computed from the normalised fields. Idempotent —
    a re-run over the same normalised record produces the same
    enriched fields.

  * **Feature-ready** — ML feature derivation. The pipeline does NOT
    compute features (that's the ML layer's job); it tags the record
    with the ``feature_ready`` flag so the ML feature store knows it
    can pull from the record without re-validating.

Quality state
-------------
Every record carries a ``quality_state`` string:

  * ``"valid"``     — passed every check, stored to vault, forwarded
    to downstream storage.
  * ``"invalid"``   — failed schema / value validation. Stored to the
    vault with ``validation_status="invalid"`` for audit; NOT
    forwarded to downstream storage.
  * ``"duplicate"`` — dedup hit. Logged; NOT stored to the vault (the
    vault's dedup UNIQUE constraint would reject the row anyway, so we
    skip the round-trip). NOT forwarded downstream.
  * ``"stale"``     — timestamp > 300s in the past. Stored to the vault
    with ``validation_status="stale"`` so a replay can see what was
    rejected for staleness; NOT forwarded downstream (a stale record
    would corrupt any ML feature pulled from it).

Concurrency
-----------
The pipeline is single-threaded by design — every connector calls
``pipeline.process(...)`` from a single ``asyncio`` task (the connector
loop), and the validator / normalizer / router stages are pure
functions (no shared mutable state). The only stateful member is the
``_processed_count`` / ``_duplicate_count`` / ``_invalid_count`` /
``_stale_count`` counters, mutated under the ``_lock`` so a future
multi-connector wave (e.g. a second book-poller instance) doesn't race
the counters.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ingestion.raw_vault import (
    DATA_VERSION_CURRENT,
    RawVault,
    raw_vault,
)


def _load_default_lineage():
    """Lazy import of the lineage tracker singleton.

    Imported lazily so the pipeline module imports cleanly even if the
    W32-4 ``ingestion.lineage`` module is unavailable (e.g. a parallel
    agent's branch hasn't landed its file yet). Mirrors the lazy-import
    pattern in ``core.book_poller._apply_book`` and the W24-4 lazy
    import inside ``_default_validator``.

    Returns ``None`` on import failure so the pipeline's best-effort
    lineage wiring no-ops rather than crashing every ``process`` call.
    """
    try:
        from ingestion.lineage import lineage_tracker
        return lineage_tracker
    except ImportError:  # pragma: no cover — defensive
        return None


logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Staleness thresholds. Mirrors the W24-4 ``data_validator`` rule:
# > 300s in the past → reject (``quality_state="stale"``).
STALE_REJECT_THRESHOLD_S = 300.0

# W32-2 — rolling-window size for the in-pipeline EPS + latency
# trackers. 1000 samples ≈ 100 s of history at 10 EPS, which is well
# beyond the 60 s throughput window the dashboard polls. Mirrors the
# ``SourceHealth.latencies`` bound in ``ingestion/health.py``.
_PIPELINE_TRACKER_MAXLEN: int = 1000


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class PipelineRecord:
    """A single record flowing through the pipeline.

    Attributes mirror the task spec's required fields (``event_time``,
    ``ingestion_time``, ``processing_time``, ``source``, ``source_id``,
    ``quality_state``, ``error_reason``) plus the ``raw_payload`` /
    ``normalized_payload`` / ``enriched_payload`` layer payloads
    themselves (so a downstream consumer can read every layer from a
    single object without re-running the pipeline).
    """

    source: str
    source_id: str
    event_type: str
    event_time: float
    ingestion_time: float
    processing_time: float = 0.0
    quality_state: str = "valid"  # valid | invalid | duplicate | stale
    error_reason: str = ""
    raw_payload: Any = None
    normalized_payload: dict[str, Any] = field(default_factory=dict)
    enriched_payload: dict[str, Any] = field(default_factory=dict)
    feature_ready: bool = False
    data_version: str = DATA_VERSION_CURRENT
    observation_id: str | None = None
    quality_score: float = 1.0


@dataclass
class PipelineResult:
    """Result of a ``Pipeline.process`` call.

    The pipeline NEVER raises — a processing error is recorded as
    ``PipelineResult.quality_state="invalid"`` with ``error_reason``
    populated. Callers that need to branch on success can check
    ``success`` (a convenience property).
    """

    record: PipelineRecord | None
    quality_state: str
    error_reason: str = ""
    observation_id: str | None = None

    @property
    def success(self) -> bool:
        """``True`` iff the record was accepted (``quality_state ==
        "valid"``) and forwarded to downstream storage."""
        return self.quality_state == "valid"


# ── Pipeline ──────────────────────────────────────────────────────────────────


class Pipeline:
    """Unified ingestion pipeline.

    Construction takes a ``RawVault`` instance (default: the module-
    level singleton) so a test can inject a fresh vault scoped to a
    ``tmp_path`` SQLite file. The optional ``validator`` /
    ``normalizer`` / ``enricher`` / ``router`` callables let a test
    override individual stages without subclassing — production wires
    the defaults (which delegate to ``core.data_validator.data_validator``
    for snapshots / trades, then compute derived fields, then route to
    ``core.database_manager.db_manager.record_snapshot`` /
    ``record_trade``).
    """

    def __init__(
        self,
        vault: RawVault | None = None,
        validator: Callable[[PipelineRecord], tuple[str, str, dict[str, Any], float]] | None = None,
        normalizer: Callable[[PipelineRecord], dict[str, Any]] | None = None,
        enricher: Callable[[PipelineRecord], dict[str, Any]] | None = None,
        router: Callable[[PipelineRecord], None] | None = None,
        lineage: Any | None = None,
    ) -> None:
        self._vault = vault or raw_vault
        self._validator = validator or _default_validator
        self._normalizer = normalizer or _default_normalizer
        self._enricher = enricher or _default_enricher
        # Router is optional — production wires ``_default_router``
        # which calls ``core.database_manager.db_manager.record_snapshot``
        # / ``record_trade`` (imported lazily so the pipeline doesn't
        # force a PG connection at import time). Tests inject a no-op
        # router to keep the test hermetic.
        self._router = router
        # W32-4 — lineage tracker. ``None`` defaults to the module-level
        # ``lineage_tracker`` singleton (loaded lazily so a missing
        # ``ingestion.lineage`` module doesn't crash the pipeline
        # import). A test injects a tmp_path-scoped tracker for
        # hermetic isolation. ``None`` here also covers the defensive
        # case where the singleton construction failed (lineage is
        # then a no-op).
        self._lineage = lineage if lineage is not None else _load_default_lineage()
        self._lock = threading.Lock()
        # Counters — surfaced via ``get_stats()``.
        self._processed_count: int = 0
        self._valid_count: int = 0
        self._invalid_count: int = 0
        self._duplicate_count: int = 0
        self._stale_count: int = 0
        self._routed_count: int = 0
        # W32-2 — lifecycle + per-source bookkeeping for the operator
        # dashboard's ``/api/status`` ingestion block and the
        # observability collector's data_source metrics. ``_running``
        # is flipped by ``start()`` / ``stop()`` (called from the
        # FastAPI lifespan); ``_sources`` is the set of unique source
        # names that have called ``process()`` since startup so
        # ``active_sources`` can answer "how many connectors are
        # wired?" without a separate registry lookup. The two deques
        # feed ``events_per_second`` / ``avg_latency_ms`` without
        # coupling the pipeline to the health monitor's per-source
        # bookkeeping (the two tracks are complementary: the health
        # monitor tracks per-source latency; the pipeline tracks
        # cross-source aggregate latency).
        self._running: bool = False
        self._sources: set[str] = set()
        self._recent_processing_times: deque[float] = deque(
            maxlen=_PIPELINE_TRACKER_MAXLEN
        )
        self._recent_latencies_ms: deque[float] = deque(
            maxlen=_PIPELINE_TRACKER_MAXLEN
        )

    # ── Public API ─────────────────────────────────────────────────────

    def process(
        self,
        source: str,
        source_id: str,
        event_type: str,
        raw_payload: Any,
        event_time: float | None = None,
    ) -> PipelineResult:
        """Process a single record through every stage.

        The pipeline is sync (no ``await`` inside any stage). A
        connector that's itself async should call this from inside a
        ``await asyncio.to_thread(...)`` offload if the validator +
        normalizer + router chain is I/O-bound (the production router
        calls the async ``db_manager.record_snapshot`` via
        ``asyncio.create_task`` so the pipeline itself stays sync).

        Args:
            source: Originating source (``"clob"`` / ``"gamma"`` / …).
            source_id: Source's own ID for the record.
            event_type: ``"snapshot"`` / ``"trade"`` / ``"order_book"``
                / ``"market_info"`` / ``"news"``.
            raw_payload: The original JSON-serialisable payload.
            event_time: When the event occurred (source-reported).
                Defaults to ``time.time()`` (the ingestion moment).

        Returns:
            ``PipelineResult`` describing the outcome. Never raises.
        """
        ing_ts = time.time()
        evt_ts = float(event_time) if event_time else ing_ts
        rec = PipelineRecord(
            source=source,
            source_id=source_id,
            event_type=event_type,
            event_time=evt_ts,
            ingestion_time=ing_ts,
            raw_payload=raw_payload,
        )

        with self._lock:
            self._processed_count += 1
            self._sources.add(source)

        # ── Stage 1: Validate ──────────────────────────────────────────
        try:
            quality_state, error_reason, normalized, quality_score = self._validator(rec)
        except Exception as e:
            # Defensive: a custom validator raised. Mark the record
            # invalid rather than propagating — the pipeline contract
            # is "never raises".
            logger.exception(
                "[pipeline] validator raised on source=%s source_id=%s: %s",
                source,
                source_id,
                e,
            )
            quality_state = "invalid"
            error_reason = f"validator raised: {type(e).__name__}: {e}"
            normalized = {}
            quality_score = 0.0

        rec.quality_state = quality_state
        rec.error_reason = error_reason
        rec.quality_score = quality_score
        rec.normalized_payload = normalized

        # ── Stage 1b: Staleness override ──────────────────────────────
        # The W24-4 ``DataValidator`` returns ``is_valid=False`` with an
        # ``errors=["Very stale data: …"]`` entry for timestamps > 300s
        # in the past; the default validator translates that into
        # ``quality_state="stale"`` here (see ``_default_validator``).
        # Belt-and-braces: re-check staleness against the normalised
        # timestamp in case the validator's staleness branch was
        # patched out by a test override.
        if quality_state == "valid" and normalized:
            norm_ts = normalized.get("timestamp")
            if isinstance(norm_ts, (int, float)) and (ing_ts - float(norm_ts)) > STALE_REJECT_THRESHOLD_S:
                rec.quality_state = "stale"
                rec.error_reason = (
                    f"Very stale data: {ing_ts - float(norm_ts):.1f}s old — "
                    "pipeline rejected at staleness override"
                )
                quality_state = "stale"

        with self._lock:
            if quality_state == "valid":
                self._valid_count += 1
            elif quality_state == "invalid":
                self._invalid_count += 1
            elif quality_state == "duplicate":
                self._duplicate_count += 1
            elif quality_state == "stale":
                self._stale_count += 1

        # ── Stage 2: Raw layer storage ────────────────────────────────
        # Every record (valid / invalid / stale) is stored to the raw
        # vault EXCEPT duplicates (the vault's UNIQUE constraint would
        # reject them anyway, so we skip the round-trip). The vault
        # returns ``observation_id`` on success or ``None`` on
        # dedup-reject / storage-error.
        if quality_state != "duplicate":
            obs_id = self._vault.record_observation(
                source=source,
                source_id=source_id,
                event_type=event_type,
                raw_payload=raw_payload,
                event_timestamp=evt_ts,
                validation_status=quality_state,
                quality_score=rec.quality_score,
                error_reason=error_reason,
                data_version=rec.data_version,
                ingestion_timestamp=ing_ts,
            )
            rec.observation_id = obs_id
            if obs_id is None and quality_state == "valid":
                # The vault rejected the row at the DB layer (UNIQUE
                # constraint hit on a record the in-memory deque
                # missed — happens after a restart). Re-classify as
                # duplicate so the downstream router skips it.
                rec.quality_state = "duplicate"
                rec.error_reason = "vault rejected (DB UNIQUE)"
                quality_state = "duplicate"
                with self._lock:
                    self._duplicate_count += 1
                    self._valid_count -= 1
            # W32-4 — record the lineage edge for this ingestion event
            # (source:clob → obs_id). Best-effort: the lineage tracker's
            # ``record_ingestion`` is wrapped in try/except so a
            # SQLite write failure (e.g. a transient ``database is
            # locked`` on the lineage.db) never breaks the pipeline.
            # Skipped for duplicates (no obs_id) and for the defensive
            # case where the lineage singleton is None (module import
            # failed).
            if obs_id is not None and self._lineage is not None:
                token_id = None
                if isinstance(normalized, dict):
                    tid = normalized.get("token_id")
                    if isinstance(tid, str) and tid:
                        token_id = tid
                # Fall back to the raw payload's token_id when the
                # normalizer hasn't run yet (invalid / stale records
                # skip the normalize stage but still carry the raw
                # payload's token_id for provenance).
                if token_id is None and isinstance(raw_payload, dict):
                    tid = raw_payload.get("token_id")
                    if isinstance(tid, str) and tid:
                        token_id = tid
                try:
                    self._lineage.record_ingestion(
                        observation_id=obs_id,
                        source=source,
                        source_id=source_id,
                        event_type=event_type,
                        token_id=token_id,
                        payload_summary=str(raw_payload)[:200],
                    )
                except Exception as e:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "[pipeline] lineage.record_ingestion failed for "
                        "obs=%s: %s",
                        obs_id,
                        e,
                    )
        else:
            rec.observation_id = None

        # ── Stages 3 + 4: Normalise + Enrich (valid records only) ─────
        if quality_state == "valid":
            try:
                rec.normalized_payload = self._normalizer(rec) or normalized
                rec.enriched_payload = self._enricher(rec)
                rec.feature_ready = bool(rec.enriched_payload)
                # W32-4 — record the lineage edges for the normalize +
                # enrich transformations. Best-effort + idempotent
                # (re-processing the same record on a replay produces
                # the same edges — the UNIQUE constraint makes the
                # second recording a no-op). Skipped when the
                # observation_id is missing (the vault rejected the
                # row) or when the lineage singleton is None.
                if rec.observation_id and self._lineage is not None:
                    norm_id = f"norm:{rec.observation_id}"
                    enr_id = f"enriched:{rec.observation_id}"
                    token_id = None
                    if isinstance(rec.normalized_payload, dict):
                        tid = rec.normalized_payload.get("token_id")
                        if isinstance(tid, str) and tid:
                            token_id = tid
                    try:
                        self._lineage.record_transformation(
                            from_id=rec.observation_id,
                            to_id=norm_id,
                            transform_type="normalize",
                            token_id=token_id,
                        )
                        if rec.enriched_payload:
                            self._lineage.record_transformation(
                                from_id=norm_id,
                                to_id=enr_id,
                                transform_type="enrich",
                                token_id=token_id,
                            )
                    except Exception as e:  # noqa: BLE001 — best-effort
                        logger.warning(
                            "[pipeline] lineage.record_transformation failed "
                            "for obs=%s: %s",
                            rec.observation_id,
                            e,
                        )
            except Exception as e:
                logger.exception(
                    "[pipeline] normalizer/enricher raised on "
                    "source=%s source_id=%s: %s",
                    source,
                    source_id,
                    e,
                )
                rec.quality_state = "invalid"
                rec.error_reason = (
                    f"normalizer/enricher raised: {type(e).__name__}: {e}"
                )
                quality_state = "invalid"
                with self._lock:
                    self._invalid_count += 1
                    self._valid_count -= 1

        # ── Stage 5: Route ─────────────────────────────────────────────
        # Only valid records are forwarded downstream. The router is
        # optional — if no router is wired (the default), the pipeline
        # just records to the vault and returns. Production wires
        # ``_default_router`` via ``Pipeline.set_router`` after the
        # ``db_manager`` is initialised (so the pipeline doesn't force
        # a PG connection at import time).
        if quality_state == "valid" and self._router is not None:
            try:
                self._router(rec)
                with self._lock:
                    self._routed_count += 1
            except Exception as e:
                logger.exception(
                    "[pipeline] router raised on source=%s source_id=%s: %s",
                    source,
                    source_id,
                    e,
                )
                # Router failure doesn't downgrade the record's
                # quality_state (the vault already has the row; a
                # replay can re-route later). The error is surfaced in
                # the result so the caller can decide whether to retry.
                rec.error_reason = f"router raised: {type(e).__name__}: {e}"

        rec.processing_time = time.time()
        # W32-2 — record the post-pipeline processing time + the
        # end-to-end pipeline latency (``processing_time -
        # ingestion_time``) so the ``events_per_second`` /
        # ``avg_latency_ms`` properties can answer "how fast is the
        # pipeline ingesting?" without coupling to the per-source
        # health monitor. Clamped at 0 to absorb clock skew (the
        # ``time.time()`` calls happen on the same wall clock, but
        # a NTP jump between the two could yield a negative delta).
        latency_ms = max(0.0, (rec.processing_time - ing_ts) * 1000.0)
        with self._lock:
            self._recent_processing_times.append(rec.processing_time)
            self._recent_latencies_ms.append(latency_ms)
        return PipelineResult(
            record=rec,
            quality_state=rec.quality_state,
            error_reason=rec.error_reason,
            observation_id=rec.observation_id,
        )

    def set_router(self, router: Callable[[PipelineRecord], None]) -> None:
        """Wire a router after construction.

        Production calls this after ``db_manager.initialize()`` so the
        router can call ``db_manager.record_snapshot`` /
        ``record_trade`` without forcing a PG connection at pipeline-
        construction time.
        """
        self._router = router

    def get_stats(self) -> dict[str, Any]:
        """Return live pipeline counters (JSON-serialisable)."""
        with self._lock:
            return {
                "processed_count": self._processed_count,
                "valid_count": self._valid_count,
                "invalid_count": self._invalid_count,
                "duplicate_count": self._duplicate_count,
                "stale_count": self._stale_count,
                "routed_count": self._routed_count,
            }

    def reset_stats(self) -> None:
        """Zero the counters (test-only — does NOT clear the vault)."""
        with self._lock:
            self._processed_count = 0
            self._valid_count = 0
            self._invalid_count = 0
            self._duplicate_count = 0
            self._stale_count = 0
            self._routed_count = 0
            self._sources.clear()
            self._recent_processing_times.clear()
            self._recent_latencies_ms.clear()

    # ── W32-2 — Lifecycle + dashboard-reading properties ────────────────

    async def start(self) -> None:
        """Mark the pipeline as running.

        The pipeline's processing path is event-driven (a connector
        calls ``process()`` per record) so ``start()`` does NOT spin up
        a background task — it just flips ``_running`` so the
        ``/api/status`` endpoint's ``pipeline_running`` field reflects
        "the lifespan has run" rather than "the module has been
        imported". Idempotent: calling ``start()`` twice is a no-op.

        Wrapped as ``async`` (mirrors the sibling ``live_fill_monitor``
        / ``trade_tape_ingester`` / ``paper_sim`` lifecycle contract)
        so the FastAPI lifespan can ``await`` every subsystem
        uniformly. ``stop()`` is the inverse.
        """
        with self._lock:
            if self._running:
                logger.debug("[pipeline] start() called but already running — no-op")
                return
            self._running = True
        logger.info("Ingestion pipeline started")

    async def stop(self) -> None:
        """Mark the pipeline as stopped.

        Does NOT cancel any in-flight ``process()`` call (the pipeline
        is synchronous — a connector holding the GIL will finish
        naturally). Idempotent: calling ``stop()`` when not running is
        a no-op. The counters + per-source set are NOT zeroed here so
        the post-shutdown ``get_stats()`` snapshot can be read by an
        operator before the process exits (mirrors the
        ``live_fill_monitor.stop()`` contract).
        """
        with self._lock:
            if not self._running:
                logger.debug("[pipeline] stop() called but not running — no-op")
                return
            self._running = False
        logger.info("Ingestion pipeline stopped")

    @property
    def is_running(self) -> bool:
        """``True`` iff ``start()`` has been called and ``stop()`` hasn't.

        Read directly under the ``_lock`` so a concurrent ``stop()``
        can't observe a half-flipped state.
        """
        with self._lock:
            return self._running

    @property
    def active_sources(self) -> int:
        """Number of unique sources that have called ``process()``.

        Returns the size of the ``_sources`` set rather than a list of
        names so the ``/api/status`` JSON stays scalar-shaped (the
        per-source detail is exposed via ``/api/ingestion/health``
        which reads the health monitor's richer per-source view).
        """
        with self._lock:
            return len(self._sources)

    @property
    def total_events(self) -> int:
        """Total records the pipeline has accepted for processing.

        Includes valid / invalid / stale / duplicate outcomes — the
        caller can subtract ``failed_count`` for the "accepted +
        forwarded" count. Mirrors the ``processed_count`` field in
        ``get_stats()``.
        """
        with self._lock:
            return self._processed_count

    @property
    def events_per_second(self) -> float:
        """Pipeline-level events-per-second over the last 60 s.

        Computed from the rolling ``_recent_processing_times`` deque
        (bounded at ``_PIPELINE_TRACKER_MAXLEN`` samples). Returns 0.0
        when no events have been processed yet or every sample in the
        deque is older than the 60 s window. Mirrors the
        ``SourceHealth.throughput()`` shape in ``ingestion/health.py``
        but cross-source (the per-source throughput lives on the
        health monitor — see ``GET /api/ingestion/health``).
        """
        now = time.time()
        with self._lock:
            if not self._recent_processing_times:
                return 0.0
            recent = [t for t in self._recent_processing_times if (now - t) <= 60.0]
        if not recent:
            return 0.0
        span = max(now - min(recent), 1.0)
        return len(recent) / span

    @property
    def avg_latency_ms(self) -> float:
        """Arithmetic mean of the most recent pipeline latencies (ms).

        Returns 0.0 when the pipeline hasn't processed any records.
        Each sample is ``processing_time - ingestion_time`` per
        record — this is the pipeline-stage latency (validate +
        normalise + enrich + route), NOT the end-to-end
        event-arrival latency (which the health monitor's
        ``last_latency`` tracks separately from ``event_time``).
        """
        with self._lock:
            if not self._recent_latencies_ms:
                return 0.0
            return sum(self._recent_latencies_ms) / len(self._recent_latencies_ms)

    @property
    def failed_count(self) -> int:
        """Records the pipeline rejected (invalid + stale + duplicate).

        ``duplicate`` is included because a duplicate is "work that
        arrived but wasn't forwarded downstream" — the operator
        dashboard's "failed records" metric should surface every
        record that didn't land in the normalized store, not just
        the schema-invalid ones.
        """
        with self._lock:
            return (
                self._invalid_count
                + self._stale_count
                + self._duplicate_count
            )


# ── Default stage implementations ─────────────────────────────────────────────


def _default_validator(rec: PipelineRecord) -> tuple[str, str, dict[str, Any], float]:
    """Validate a record and return ``(quality_state, error_reason,
    normalized_payload, quality_score)``.

    Delegates snapshot / trade validation to the existing
    ``core.data_validator.data_validator`` singleton (W24-4) so the
    validation rules don't drift between the W24-4 path and the W31-1
    pipeline path. Other event types (``order_book`` / ``market_info``
    / ``news``) skip the W24-4 validator (which only knows snapshots +
    trades) and pass through with a default ``valid`` verdict (the raw
    vault stores them regardless; a downstream normalizer can apply
    type-specific checks in a future wave).

    The dedup check (W24-4 returns ``is_duplicate=True`` for snapshots
    / trades we've already seen) is translated to ``quality_state
    ="duplicate"`` so the pipeline's ``process`` skips the raw-vault
    write entirely (the vault's UNIQUE constraint would reject the row
    anyway, so the round-trip is wasted).
    """
    payload = rec.raw_payload
    if not isinstance(payload, dict):
        # Non-dict payloads (e.g. a list of trades from a bulk fetch)
        # are accepted with a neutral verdict — the caller is expected
        # to fan them out into individual records before calling
        # ``process``.
        return ("valid", "", {}, 1.0)

    # Lazy import so the pipeline module imports cleanly even if the
    # W24-4 ``data_validator`` is unavailable (e.g. a parallel agent's
    # branch hasn't landed its file yet). Mirrors the pattern in
    # ``core/book_poller.py::_apply_book``.
    try:
        from core.data_validator import data_validator
    except ImportError:  # pragma: no cover — defensive
        data_validator = None

    event_type = rec.event_type
    if event_type == "snapshot" and data_validator is not None:
        result = data_validator.validate_snapshot(payload)
    elif event_type == "trade" and data_validator is not None:
        result = data_validator.validate_trade(payload)
    else:
        # Non-snapshot / non-trade event types (``order_book`` /
        # ``market_info`` / ``news``) skip the W24-4 validator. The
        # raw layer still stores them; downstream normalisers can
        # apply type-specific checks.
        return ("valid", "", dict(payload), 1.0)

    if result.is_duplicate:
        return ("duplicate", "duplicate (W24-4 validator)", {}, 0.0)
    if not result.is_valid:
        # Inspect the errors to decide between ``invalid`` (schema /
        # value errors) and ``stale`` (the very-stale branch).
        errors = result.errors or []
        if any("Very stale" in e for e in errors):
            return ("stale", "; ".join(errors), {}, 0.0)
        return ("invalid", "; ".join(errors), {}, 0.0)
    return ("valid", "", result.normalized_data, 1.0)


def _default_normalizer(rec: PipelineRecord) -> dict[str, Any]:
    """Normalise the payload — coerce timestamps to float, prices /
    sizes to float, sides to upper-case. The W24-4 validator already
    does this for snapshots / trades (the result is in
    ``rec.normalized_payload`` after the validator stage); this
    function is a pass-through for those event types and a no-op
    identity for others.
    """
    # If the validator already populated ``normalized_payload``, pass
    # it through (the W24-4 validator's output IS the normalised
    # payload — re-running would double the work).
    if rec.normalized_payload:
        return dict(rec.normalized_payload)
    if isinstance(rec.raw_payload, dict):
        return dict(rec.raw_payload)
    return {}


def _default_enricher(rec: PipelineRecord) -> dict[str, Any]:
    """Compute derived fields (``mid``, ``spread``, ``depth_10``).

    For snapshots / trades the W24-4 validator already computes
    ``mid`` / ``spread``; this enricher adds ``depth_10`` when the
    raw payload includes a full order book (the book poller's
    ``OrderBook`` carries ``bid_depth_10`` / ``ask_depth_10``). For
    other event types the enricher is a no-op (the ML feature store
    derives its own features from the normalised payload).
    """
    enriched: dict[str, Any] = {}
    norm = rec.normalized_payload
    if not norm:
        return enriched
    # Carry forward derived fields the validator already computed.
    if "mid" in norm:
        enriched["mid"] = norm["mid"]
    if "spread" in norm:
        enriched["spread"] = norm["spread"]
    # Pull depth from the raw payload if present (the W24-4 validator
    # doesn't compute depth — it only sees the top-of-book).
    raw = rec.raw_payload
    if isinstance(raw, dict):
        bid_depth = raw.get("bid_depth_10") or raw.get("bid_depth")
        ask_depth = raw.get("ask_depth_10") or raw.get("ask_depth")
        if bid_depth is not None:
            try:
                enriched["bid_depth_10"] = float(bid_depth)
            except (TypeError, ValueError):
                pass
        if ask_depth is not None:
            try:
                enriched["ask_depth_10"] = float(ask_depth)
            except (TypeError, ValueError):
                pass
        if "bid_depth_10" in enriched and "ask_depth_10" in enriched:
            enriched["total_depth_10"] = (
                enriched["bid_depth_10"] + enriched["ask_depth_10"]
            )
    return enriched


# ── Module-level singleton ────────────────────────────────────────────────────
# Mirrors the convention used by every sibling background-task module.
# Construction does NOT wire a router — production calls
# ``pipeline.set_router(_default_router)`` after the ``db_manager`` is
# initialised (so the pipeline doesn't force a PG connection at import
# time). Tests construct a fresh ``Pipeline(vault=...)`` per test.
pipeline = Pipeline()

# W32-2 — operator-facing alias. ``api/server.py``'s lifespan imports the
# pipeline under the name ``ingestion_pipeline`` (mirrors the
# ``ingestion_health_monitor`` naming used by the sibling health module)
# so the startup / shutdown block reads as a uniform ``ingestion_*``
# pair. Both names reference the same singleton instance — there's no
# second ``Pipeline()`` construction.
ingestion_pipeline = pipeline


__all__ = [
    "Pipeline",
    "PipelineRecord",
    "PipelineResult",
    "pipeline",
    "ingestion_pipeline",
    "STALE_REJECT_THRESHOLD_S",
]
