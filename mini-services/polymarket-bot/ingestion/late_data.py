"""Late-arriving data handler — handles data that arrives after its event time.

Scenarios:
1. Trade fill arrives 30s after execution
2. Market resolution arrives hours after market closed
3. Order book update arrives out of order
4. Price correction from exchange

Handling:
1. Detect late arrival (ingestion_time - event_time > threshold)
2. Record the late arrival
3. Update normalized/enriched layers
4. Log correction
5. Alert if late data rate is high
6. Ensure ML features use point-in-time data (not late arrivals for past
   predictions)

W35-4 — Late data + corrections.

The handler is SQLite-backed (a dedicated db file at ``LATE_DATA_DB_PATH``,
defaulting to ``/app/data/late_data.db``) so the late-arrival log + the
correction log BOTH survive process restarts and can be queried out-of-band
by an operator / dashboard. Mirrors the persistence convention of the
sibling ``ingestion.raw_vault`` / ``ingestion.dead_letter`` /
``ingestion.lineage`` modules — a dedicated SQLite file separate from every
other store so an ingestion hiccup can never perturb the late-data audit
trail.

Schema
------
Two tables share the SQLite file:

  * ``late_arrivals`` — one row per detected late-arriving record. The
    ``(source, source_id, event_time)`` triple identifies the upstream
    observation (the same ``observation_id`` from the raw vault is
    carried as ``observation_id`` when known). ``lateness_seconds`` is
    ``ingestion_time - event_time`` (clamped at 0 — a negative value
    would imply the event arrived before it happened, which is clock
    skew, not lateness).

  * ``corrections`` — one row per applied correction. Each row carries
    the field path that was corrected (``"token_id.X.best_bid"``), the
    OLD value, the NEW value, the correction reason (``"exchange_cancel"``
    / ``"reconciliation"`` / ``"schema_migration"`` …), and the actor
    that applied it (``"system"`` / ``"operator:<user>"``). The
    ``observation_id`` joins back to the raw vault row that was
    corrected so an operator can walk from the correction to the
    underlying observation.

Pipeline wiring
---------------
``Pipeline.process`` calls ``late_data_handler.record_late_arrival(...)``
AFTER the record is stored to the raw vault (so the late-arrival log
reflects records that actually landed in the audit trail, not records
that were dropped at dedup). The call is best-effort — a SQLite write
failure is logged at WARNING level and swallowed so the pipeline never
breaks because of the late-data handler (mirrors the raw_vault's
``record_observation`` best-effort contract).

Point-in-time safety
--------------------
ML feature pipelines MUST NOT use late-arriving data for past
predictions — doing so would leak future information into a backtest
and inflate reported performance. The handler exposes
``is_safe_for_pit(observation_id, as_of)`` / ``filter_pit_safe(records,
as_of)`` so the feature store can ask "was this record ingested before
``as_of``?" without re-querying the raw vault. A record is PIT-safe iff
its ``ingestion_time <= as_of`` — late arrivals ingested AFTER ``as_of``
are filtered out, even if their ``event_time`` is earlier (the canonical
"did we know about this at time T?" check).

Concurrency
-----------
The handler is single-threaded by design — every call site
(``ingestion.pipeline.Pipeline.process`` / API routes) is either a
sync caller or a single ``asyncio`` task. SQLite's default ``BEGIN
IMMEDIATE`` transaction gives serialisability across threads, and the
``check_same_thread=False`` flag lets the handler be shared between the
asyncio loop and ``asyncio.to_thread`` offloads without a
``ProgrammingError``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Default DB path. The conftest redirects this in tests; production resolves
# it to ``/app/data/late_data.db`` (the docker-compose volume bind-mount
# makes ``/app/data`` writable in the prod container).
DEFAULT_DB_PATH = "/app/data/late_data.db"

# Lateness detection threshold (seconds). A record is "late" iff
# ``ingestion_time - event_time > LATE_THRESHOLD_S``. 30 s mirrors the
# task spec's "trade fill arrives 30 s after execution" scenario — short
# enough to catch real lag, long enough that normal processing jitter
# (10–50 ms per record) doesn't false-positive on every record.
#
# Deliberately BELOW the W31-1 ``STALE_REJECT_THRESHOLD_S = 300.0`` so
# there's a meaningful "late but not stale" band: a record between 30 s
# and 300 s late is logged as late (here) but still flows downstream;
# a record > 300 s late is rejected by the pipeline's staleness
# override entirely.
LATE_THRESHOLD_S: float = 30.0

# Rolling-window size for the in-memory late-rate tracker. 1000 samples
# ≈ 100 s of history at 10 EPS, which is plenty to compute a stable
# late-rate over the last minute for the alert heuristic. Mirrors the
# ``_PIPELINE_TRACKER_MAXLEN`` convention in ``ingestion.pipeline``.
_LATE_RATE_TRACKER_MAXLEN: int = 1000

# Alert threshold — when the rolling late-rate over the last
# ``_LATE_RATE_TRACKER_MAXLEN`` samples exceeds this fraction, the
# handler fires a ``late_data_rate_high`` alert (best-effort —
# ``core.alerting`` is imported lazily). 0.20 = 20 % of records being
# late is a clear signal that the upstream source is degraded.
LATE_RATE_ALERT_THRESHOLD: float = 0.20

# Canonical correction-reason vocabulary. Kept as a frozenset for the
# ``record_correction`` precondition check; the DB layer accepts any
# TEXT value so a future reason (e.g. ``"gdpr_redaction"``) doesn't
# require a migration. The set is documented so a downstream
# visualisation can switch on it without fearing churn.
CORRECTION_REASONS = frozenset({
    "exchange_cancel",         # exchange cancelled / busted a fill
    "exchange_correction",     # exchange sent a corrected price
    "reconciliation",          # internal reconciliation found a mismatch
    "schema_migration",        # a schema migration rewrote a value
    "manual_override",         # operator manually corrected a value
    "late_fill",               # a trade fill arrived after its execution
    "resolution_late",         # market resolution arrived hours late
    "out_of_order",            # order book update arrived out of order
    "other",                   # catch-all for un-categorised corrections
})


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class LateArrival:
    """A single late-arriving record detected by the handler.

    Attributes:
        late_id: UUID4 hex — primary key.
        observation_id: The raw-vault ``observation_id`` of the late
            record (when known). Empty string when the handler is
            called outside the pipeline path (e.g. a direct call from
            a connector that doesn't yet have a vault row).
        source: Originating source (``"clob"`` / ``"gamma"`` / …).
        source_id: The source's own ID for the record (e.g. trade_id).
        event_type: ``"snapshot"`` / ``"trade"`` / ``"order_book"`` /
            ``"market_info"`` / ``"news"``.
        event_time: When the event occurred (source-reported), as a
            Unix timestamp.
        ingestion_time: When the vault received the record (``time.time()``
            at the top of ``record_observation``).
        lateness_seconds: ``ingestion_time - event_time`` (clamped at
            0 — a negative value would imply clock skew, not lateness).
        token_id: Polymarket market token id (when applicable). Indexed
            so a per-market late-arrival query is O(log n).
        metadata: Free-form JSON-serialisable dict for caller-supplied
            context (e.g. ``{"original_payload": {...}}``).
        recorded_at: Unix timestamp when the late arrival was logged
            by this handler.
    """

    late_id: str
    observation_id: str
    source: str
    source_id: str
    event_type: str
    event_time: float
    ingestion_time: float
    lateness_seconds: float
    token_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    recorded_at: float = 0.0


@dataclass
class Correction:
    """A single correction applied to a previously-stored record.

    Attributes:
        correction_id: UUID4 hex — primary key.
        observation_id: The raw-vault ``observation_id`` of the record
            that was corrected. Indexed so a per-record correction
            history query is O(log n).
        source: Originating source of the corrected record.
        source_id: The source's own ID for the record.
        field_path: The field that was corrected, as a dotted path
            (e.g. ``"best_bid"`` / ``"payload.trades[0].price"``).
        old_value: The previous value (JSON-serialised; ``None`` is
            represented as the string ``"null"`` so it round-trips
            through SQLite TEXT cleanly).
        new_value: The corrected value (same JSON convention).
        reason: One of ``CORRECTION_REASONS`` (free-form TEXT in the
            DB so a future reason doesn't require a migration).
        actor: Who applied the correction (``"system"`` /
            ``"operator:<user>"`` / ``"pipeline:<stage>"``).
        metadata: Free-form JSON-serialisable dict for caller-supplied
            context.
        corrected_at: Unix timestamp when the correction was applied.
    """

    correction_id: str
    observation_id: str
    source: str
    source_id: str
    field_path: str
    old_value: Any
    new_value: Any
    reason: str
    actor: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)
    corrected_at: float = 0.0


# ── Handler ──────────────────────────────────────────────────────────────────


class LateDataHandler:
    """SQLite-backed late-arrival log + correction log.

    Construction is import-safe — the SQLite ``_init_db`` is wrapped in
    a try/except so an unwritable default path (``/app/data`` is
    read-only in the sandbox) doesn't crash the import. On failure the
    handler falls back to ``/tmp/late_data.db`` (mirrors the
    ``raw_vault`` / ``lineage`` fallback convention) and logs a WARNING
    so the operator sees the redirect.

    The class is safe to instantiate without env vars / paths set —
    the constructor falls back to ``DEFAULT_DB_PATH`` and creates the
    parent directory if missing. ``/app/data`` is the prod default
    (writable in the docker-compose volume); tests redirect via the
    ``LATE_DATA_DB_PATH`` env var (handled by ``tests/conftest.py``).
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        late_threshold_s: float = LATE_THRESHOLD_S,
        alert_threshold: float = LATE_RATE_ALERT_THRESHOLD,
    ) -> None:
        path_str = (
            db_path
            or os.environ.get("LATE_DATA_DB_PATH", DEFAULT_DB_PATH)
        )
        self._db_path = Path(path_str)
        self._late_threshold_s = float(late_threshold_s)
        self._alert_threshold = float(alert_threshold)
        # ``check_same_thread=False`` so the handler can be shared
        # between the asyncio event loop thread and ``asyncio.to_thread``
        # offloads. SQLite's default ``BEGIN IMMEDIATE`` transaction
        # gives serialisability across the GIL boundary; the ``_lock``
        # is for in-process serialisation on the (rare) multi-thread
        # call sites.
        self._lock = threading.Lock()
        # Counters — surfaced via ``get_stats`` for the operator
        # dashboard / observability layer.
        self._late_count: int = 0
        self._correction_count: int = 0
        self._alert_fired_count: int = 0
        # Rolling late-rate tracker — appended on every
        # ``detect_late_arrival`` call (1.0 for late, 0.0 for on-time).
        # Bounded at ``_LATE_RATE_TRACKER_MAXLEN`` samples so a long-
        # running process doesn't grow the deque unbounded. Mirrors the
        # ``ingestion.pipeline.Pipeline._recent_latencies_ms`` pattern.
        self._recent_late_flags: deque[float] = deque(
            maxlen=_LATE_RATE_TRACKER_MAXLEN
        )
        self._init_db()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the ``late_arrivals`` + ``corrections`` tables if missing.

        Idempotent so repeated ``LateDataHandler()`` constructions
        against the same db file are safe. Parent directory is
        auto-created so a fresh sandbox with no ``/app/data`` directory
        works (mirrors ``raw_vault._init_db`` / ``lineage._init_db``).
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Defensive: if the parent isn't writable, fall back to
            # ``/tmp/late_data.db`` so the bot still boots. A logged
            # warning surfaces the redirect; the operator can fix the
            # path env var. Mirrors the raw_vault / lineage fallback
            # convention.
            logger.warning(
                "[late_data] Cannot create parent dir %s: %s — "
                "falling back to /tmp/late_data.db",
                self._db_path.parent,
                e,
            )
            self._db_path = Path("/tmp/late_data.db")

        try:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS late_arrivals (
                        late_id           TEXT    PRIMARY KEY,
                        observation_id    TEXT    NOT NULL DEFAULT '',
                        source            TEXT    NOT NULL,
                        source_id         TEXT    NOT NULL,
                        event_type        TEXT    NOT NULL,
                        event_time        REAL    NOT NULL,
                        ingestion_time    REAL    NOT NULL,
                        lateness_seconds  REAL    NOT NULL,
                        token_id          TEXT    NOT NULL DEFAULT '',
                        metadata          TEXT    NOT NULL DEFAULT '{}',
                        recorded_at       REAL    NOT NULL
                    )
                """)
                # ``(source, event_time DESC)`` — the per-source recent-
                # late-arrivals query (used by the API endpoint + the
                # late-rate alert heuristic) narrows by source + orders
                # by event_time, so a composite index avoids a full scan.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_late_source_time
                    ON late_arrivals (source, event_time DESC)
                """)
                # ``(token_id)`` — per-market late-arrival drill-down.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_late_token
                    ON late_arrivals (token_id)
                """)
                # ``(recorded_at DESC)`` — the API endpoint's default
                # "most recent first" ordering.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_late_recorded
                    ON late_arrivals (recorded_at DESC)
                """)
                # ``(observation_id)`` — joining late arrivals back to
                # the raw vault row.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_late_obs
                    ON late_arrivals (observation_id)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS corrections (
                        correction_id     TEXT    PRIMARY KEY,
                        observation_id    TEXT    NOT NULL DEFAULT '',
                        source            TEXT    NOT NULL DEFAULT '',
                        source_id         TEXT    NOT NULL DEFAULT '',
                        field_path        TEXT    NOT NULL,
                        old_value         TEXT    NOT NULL DEFAULT '',
                        new_value         TEXT    NOT NULL DEFAULT '',
                        reason            TEXT    NOT NULL,
                        actor             TEXT    NOT NULL DEFAULT 'system',
                        metadata          TEXT    NOT NULL DEFAULT '{}',
                        corrected_at      REAL    NOT NULL
                    )
                """)
                # ``(corrected_at DESC)`` — the API endpoint's default
                # "most recent first" ordering.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_corr_corrected
                    ON corrections (corrected_at DESC)
                """)
                # ``(observation_id)`` — per-record correction history.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_corr_obs
                    ON corrections (observation_id)
                """)
                # ``(reason)`` — per-reason breakdown for the dashboard.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_corr_reason
                    ON corrections (reason)
                """)
                conn.commit()
        except sqlite3.Error as e:
            # Defensive: a schema-init failure (corrupted db / locked
            # schema / etc.) is logged at ERROR but doesn't raise so
            # the bot still boots. Subsequent writes will fail and be
            # logged per-call; reads will return empty lists. Mirrors
            # the ``dead_letter._init_db`` defensive convention.
            logger.error(
                "[late_data] _init_db failed (path=%s): %s",
                self._db_path,
                e,
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection (the handler doesn't pool — SQLite
        file open is cheap, and a per-call connection prevents the
        ``database is locked`` errors that a long-lived connection
        would surface under contention). Mirrors ``raw_vault._connect``.
        """
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=5.0,
            isolation_level="IMMEDIATE",
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ── Public API: detection ──────────────────────────────────────────

    def detect_late_arrival(
        self,
        event_time: float,
        ingestion_time: float | None = None,
        threshold: float | None = None,
    ) -> bool:
        """Return ``True`` iff ``ingestion_time - event_time > threshold``.

        Args:
            event_time: When the event occurred (source-reported).
            ingestion_time: When the record was ingested. Defaults to
                ``time.time()`` (the typical case at the top of
                ``Pipeline.process``).
            threshold: Override the handler's default
                ``late_threshold_s``. ``None`` (default) uses the
                constructor's value.

        Returns:
            ``True`` when the record is late, ``False`` otherwise.
            Negative lateness (clock skew — ``event_time`` is in the
            future relative to ``ingestion_time``) is treated as
            NOT late (a record can't be "late" if it arrived before
            it happened).
        """
        ing_ts = float(ingestion_time) if ingestion_time is not None else time.time()
        evt_ts = float(event_time)
        thresh = float(threshold) if threshold is not None else self._late_threshold_s
        lateness = ing_ts - evt_ts
        is_late = lateness > thresh
        # Track the late-flag in the rolling window so the alert
        # heuristic can compute a stable late-rate over the last N
        # samples without re-querying the DB.
        with self._lock:
            self._recent_late_flags.append(1.0 if is_late else 0.0)
        return is_late

    # ── Public API: late-arrival recording ──────────────────────────────

    def record_late_arrival(
        self,
        *,
        source: str,
        source_id: str,
        event_type: str,
        event_time: float,
        ingestion_time: float | None = None,
        observation_id: str = "",
        token_id: str = "",
        metadata: dict[str, Any] | None = None,
        threshold: float | None = None,
    ) -> str:
        """Record a single late-arriving record.

        Called by ``Pipeline.process`` AFTER the record is stored to
        the raw vault (so the late-arrival log reflects records that
        actually landed in the audit trail). The call is best-effort —
        a SQLite write failure is logged at WARNING level and swallowed
        so the pipeline never breaks because of the late-data handler
        (mirrors the raw_vault's ``record_observation`` best-effort
        contract).

        Args:
            source: Originating source (``"clob"`` / ``"gamma"`` …).
            source_id: The source's own ID for the record.
            event_type: ``"snapshot"`` / ``"trade"`` / etc.
            event_time: When the event occurred (source-reported).
            ingestion_time: When the record was ingested. Defaults to
                ``time.time()``.
            observation_id: The raw-vault ``observation_id`` of the
                late record (when known). Empty string when called
                outside the pipeline path.
            token_id: Polymarket market token id (when applicable).
            metadata: Free-form JSON-serialisable dict for caller-
                supplied context.
            threshold: Override the handler's default
                ``late_threshold_s`` for this call (a record below
                the threshold is NOT logged — ``record_late_arrival``
                returns an empty string and increments no counter).

        Returns:
            The new ``late_id`` (UUID4 hex) — or empty string on
            failure (the underlying SQLite write is wrapped in
            try/except so an I/O hiccup never breaks the caller) or
            when the record was below the lateness threshold (the
            handler treats that as "not late, nothing to log").
        """
        ing_ts = float(ingestion_time) if ingestion_time is not None else time.time()
        evt_ts = float(event_time)
        thresh = float(threshold) if threshold is not None else self._late_threshold_s
        lateness = max(0.0, ing_ts - evt_ts)
        if lateness <= thresh:
            # Not late — nothing to record. The caller already called
            # ``detect_late_arrival`` (which appended a 0.0 flag to
            # the rolling window) so the late-rate tracker stays in
            # sync. Return an empty string so the caller can branch
            # on "was a late row logged?".
            return ""

        late_id = uuid.uuid4().hex
        recorded_at = time.time()
        meta_json = json.dumps(metadata or {}, default=str, sort_keys=True)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO late_arrivals
                    (late_id, observation_id, source, source_id,
                     event_type, event_time, ingestion_time,
                     lateness_seconds, token_id, metadata, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        late_id,
                        observation_id,
                        source,
                        source_id,
                        event_type,
                        evt_ts,
                        ing_ts,
                        lateness,
                        token_id,
                        meta_json,
                        recorded_at,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            # Defensive: a write failure (disk full / locked db /
            # corrupted schema) must NOT break the caller — the
            # late-arrival log is best-effort audit, not a critical
            # path. Mirrors the ``raw_vault.record_observation`` /
            # ``dead_letter.add`` defensive convention.
            logger.warning(
                "[late_data] record_late_arrival failed (source=%s "
                "source_id=%s): %s",
                source,
                source_id,
                e,
            )
            return ""
        with self._lock:
            self._late_count += 1
        logger.info(
            "[late_data] late arrival recorded: source=%s source_id=%s "
            "event_type=%s lateness=%.2fs observation_id=%s",
            source,
            source_id,
            event_type,
            lateness,
            observation_id,
        )
        # Best-effort alert — fires when the rolling late-rate crosses
        # the configured threshold. Wrapped in try/except so an alert
        # failure can never break the recording path.
        try:
            self._maybe_fire_alert()
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.debug(
                "[late_data] late-rate alert check failed (continuing): %s",
                e,
            )
        return late_id

    def get_late_arrivals(
        self,
        limit: int = 50,
        source: str | None = None,
        token_id: str | None = None,
    ) -> list[LateArrival]:
        """Return recent late arrivals (most-recent-first by ``recorded_at``).

        Args:
            limit: Maximum records to return (default 50, hard ceiling
                1000 so a misbehaving caller can't OOM the bot).
            source: Optional source filter.
            token_id: Optional token filter.

        Returns:
            List of ``LateArrival`` (empty list on failure).
        """
        cap = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM late_arrivals"
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if token_id is not None:
            clauses.append("token_id = ?")
            params.append(token_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(cap)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.error("[late_data] get_late_arrivals failed: %s", e)
            return []
        return [self._row_to_late_arrival(r) for r in rows]

    # ── Public API: correction logging ─────────────────────────────────

    def record_correction(
        self,
        *,
        observation_id: str,
        field_path: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        source: str = "",
        source_id: str = "",
        actor: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Log a correction applied to a previously-stored record.

        Tracks:
          * WHAT was corrected (``field_path``)
          * WHEN it was corrected (``corrected_at = time.time()``)
          * WHAT the OLD value was (``old_value``)
          * WHAT the NEW value is (``new_value``)
          * WHY it was corrected (``reason`` — one of
            ``CORRECTION_REASONS``)

        The ``old_value`` / ``new_value`` are JSON-serialised with
        ``default=str`` so non-JSON-serialisable objects (``Decimal`` /
        ``datetime``) round-trip cleanly through SQLite TEXT. A value
        of ``None`` is serialised as the JSON token ``"null"`` (NOT
        the empty string) so a caller can distinguish "no old value"
        from "old value was None".

        Args:
            observation_id: The raw-vault ``observation_id`` of the
                record that was corrected.
            field_path: The field that was corrected, as a dotted path
                (e.g. ``"best_bid"`` / ``"payload.trades[0].price"``).
            old_value: The previous value.
            new_value: The corrected value.
            reason: One of ``CORRECTION_REASONS``. A non-canonical
                value is logged at WARNING but still stored (the DB
                layer accepts any TEXT) so the correction audit trail
                never loses a row to a vocabulary drift.
            source: Originating source of the corrected record.
            source_id: The source's own ID for the record.
            actor: Who applied the correction (``"system"`` /
                ``"operator:<user>"`` / ``"pipeline:<stage>"``).
            metadata: Free-form JSON-serialisable dict for caller-
                supplied context.

        Returns:
            The new ``correction_id`` (UUID4 hex) — or empty string on
            failure.
        """
        if reason not in CORRECTION_REASONS:
            logger.warning(
                "[late_data] non-canonical correction reason %r — "
                "storing anyway (DB accepts free-form TEXT)",
                reason,
            )
        correction_id = uuid.uuid4().hex
        corrected_at = time.time()
        meta_json = json.dumps(metadata or {}, default=str, sort_keys=True)
        old_json = json.dumps(old_value, default=str, sort_keys=True)
        new_json = json.dumps(new_value, default=str, sort_keys=True)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO corrections
                    (correction_id, observation_id, source, source_id,
                     field_path, old_value, new_value, reason, actor,
                     metadata, corrected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        correction_id,
                        observation_id,
                        source,
                        source_id,
                        field_path,
                        old_json,
                        new_json,
                        reason,
                        actor,
                        meta_json,
                        corrected_at,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.warning(
                "[late_data] record_correction failed (observation_id=%s "
                "field_path=%s): %s",
                observation_id,
                field_path,
                e,
            )
            return ""
        with self._lock:
            self._correction_count += 1
        logger.info(
            "[late_data] correction recorded: observation_id=%s "
            "field_path=%s reason=%s actor=%s",
            observation_id,
            field_path,
            reason,
            actor,
        )
        return correction_id

    def get_corrections(
        self,
        limit: int = 50,
        observation_id: str | None = None,
        reason: str | None = None,
    ) -> list[Correction]:
        """Return recent corrections (most-recent-first by ``corrected_at``).

        Args:
            limit: Maximum records to return (default 50, hard ceiling
                1000).
            observation_id: Optional filter — only corrections for the
                given raw-vault row.
            reason: Optional filter — only corrections with the given
                reason.

        Returns:
            List of ``Correction`` (empty list on failure).
        """
        cap = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM corrections"
        clauses: list[str] = []
        params: list[Any] = []
        if observation_id is not None:
            clauses.append("observation_id = ?")
            params.append(observation_id)
        if reason is not None:
            clauses.append("reason = ?")
            params.append(reason)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY corrected_at DESC LIMIT ?"
        params.append(cap)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.error("[late_data] get_corrections failed: %s", e)
            return []
        return [self._row_to_correction(r) for r in rows]

    # ── Public API: point-in-time safety ───────────────────────────────

    def is_safe_for_pit(
        self,
        observation_id: str,
        as_of: float,
    ) -> bool | None:
        """Return ``True`` iff the record was ingested at or before ``as_of``.

        Used by the ML feature store to enforce point-in-time safety:
        a feature computation for prediction-time ``T`` may only see
        records whose ``ingestion_time <= T``. Late arrivals ingested
        AFTER ``T`` are NOT PIT-safe — they would leak future
        information into the prediction.

        Args:
            observation_id: The raw-vault ``observation_id`` of the
                record to check.
            as_of: The prediction-time cutoff (Unix timestamp).

        Returns:
            ``True`` when the record was ingested at or before
            ``as_of`` (PIT-safe). ``False`` when the record was ingested
            after ``as_of`` (NOT PIT-safe — would leak future info).
            ``None`` when the observation_id isn't in the late-arrivals
            log (the record was either on-time OR never recorded — the
            caller should fall back to the raw vault's
            ``ingestion_timestamp`` to decide; ``None`` signals "I
            don't know, ask the vault").
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ingestion_time FROM late_arrivals
                    WHERE observation_id = ?
                    ORDER BY ingestion_time DESC
                    LIMIT 1
                    """,
                    (observation_id,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.error(
                "[late_data] is_safe_for_pit failed (observation_id=%s): %s",
                observation_id,
                e,
            )
            return None
        if row is None:
            return None
        return float(row["ingestion_time"]) <= float(as_of)

    def filter_pit_safe(
        self,
        records: Iterable[dict[str, Any]],
        as_of: float,
        ingestion_time_key: str = "ingestion_time",
    ) -> list[dict[str, Any]]:
        """Filter ``records`` to only those ingested at or before ``as_of``.

        Convenience wrapper around the per-record PIT-safety check for
        a list of record dicts (the shape returned by
        ``raw_vault.replay_range``). Records whose ``ingestion_time_key``
        is missing or non-numeric are DROPPED (a record without an
        ingestion timestamp can't be PIT-validated, so the safest
        interpretation is "exclude" — better to under-train an ML
        model than to leak future data).

        Args:
            records: Iterable of record dicts (e.g. the output of
                ``raw_vault.replay_range``).
            as_of: The prediction-time cutoff (Unix timestamp).
            ingestion_time_key: The dict key that holds the record's
                ingestion timestamp. Defaults to ``"ingestion_time"``
                (the raw vault's column name).

        Returns:
            A list of records whose ingestion timestamp is at or
            before ``as_of``.
        """
        cutoff = float(as_of)
        safe: list[dict[str, Any]] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            ing_ts = rec.get(ingestion_time_key)
            if not isinstance(ing_ts, (int, float)):
                # Missing / non-numeric ingestion timestamp — can't
                # validate PIT safety, exclude (the safest choice).
                continue
            if float(ing_ts) <= cutoff:
                safe.append(rec)
        return safe

    # ── Public API: stats + alerting ───────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of the handler state.

        Returned keys: ``late_count`` (cumulative late arrivals logged
        in this process), ``correction_count`` (cumulative corrections
        logged), ``late_threshold_s`` (the configured threshold),
        ``late_rate`` (rolling late-rate over the last
        ``_LATE_RATE_TRACKER_MAXLEN`` detection calls),
        ``alert_threshold`` (the configured late-rate alert threshold),
        ``alert_fired_count`` (cumulative alerts fired in this
        process), ``db_path`` (the SQLite file the handler is writing
        to).
        """
        with self._lock:
            late_rate = (
                sum(self._recent_late_flags) / len(self._recent_late_flags)
                if self._recent_late_flags
                else 0.0
            )
            return {
                "late_count": self._late_count,
                "correction_count": self._correction_count,
                "late_threshold_s": self._late_threshold_s,
                "late_rate": round(late_rate, 4),
                "late_rate_samples": len(self._recent_late_flags),
                "alert_threshold": self._alert_threshold,
                "alert_fired_count": self._alert_fired_count,
                "db_path": str(self._db_path),
            }

    def reset_stats(self) -> None:
        """Zero the in-memory counters + clear the late-rate deque.

        Test-only — does NOT truncate the on-disk SQLite tables (the
        handler has no public truncate method by design — every record
        survives for audit, mirroring the ``raw_vault`` convention).
        Mirrors the ``raw_vault.reset_stats`` / ``pipeline.reset_stats``
        pattern in ``tests/conftest.py``'s autouse fixture.
        """
        with self._lock:
            self._late_count = 0
            self._correction_count = 0
            self._alert_fired_count = 0
            self._recent_late_flags.clear()

    def _maybe_fire_alert(self) -> None:
        """Fire a ``late_data_rate_high`` alert when the rolling
        late-rate exceeds the configured threshold.

        Best-effort — ``core.alerting`` is imported lazily so the
        handler can be imported in environments where the alert engine
        is not yet ready (e.g. unit tests). The alert is fire-and-
        forget — any exception is swallowed so an alerting hiccup can
        never break the late-arrival recording path. Mirrors the
        ``dead_letter._fire_alert`` defensive convention.
        """
        with self._lock:
            if not self._recent_late_flags:
                return
            late_rate = (
                sum(self._recent_late_flags)
                / len(self._recent_late_flags)
            )
            if late_rate < self._alert_threshold:
                return
            # Mark the alert as fired BEFORE the lazy import so a
            # failure in the alert engine doesn't cause a re-fire
            # storm on the next call (the counter is monotonic).
            self._alert_fired_count += 1
        try:
            from core.alerting import SEVERITY_WARNING, alert_engine

            alert_engine.record_alert(
                name="late_data_rate_high",
                category="data",
                severity=SEVERITY_WARNING,
                message=(
                    f"Late-data rate {late_rate:.1%} exceeds threshold "
                    f"{self._alert_threshold:.1%} — upstream source may "
                    f"be degraded"
                ),
                metadata={
                    "late_rate": round(late_rate, 4),
                    "threshold": self._alert_threshold,
                    "samples": len(self._recent_late_flags),
                },
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.debug(
                "[late_data] late-rate alert fire failed (continuing): %s",
                e,
            )

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_late_arrival(row: sqlite3.Row) -> LateArrival:
        """Convert a ``late_arrivals`` row into a ``LateArrival`` dataclass.

        Defensive on JSON deserialisation — a corrupt ``metadata`` cell
        (e.g. truncated by a disk-full condition) falls back to ``{}``
        rather than raising.
        """
        try:
            metadata = json.loads(row["metadata"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        return LateArrival(
            late_id=row["late_id"],
            observation_id=row["observation_id"] or "",
            source=row["source"],
            source_id=row["source_id"],
            event_type=row["event_type"],
            event_time=float(row["event_time"] or 0.0),
            ingestion_time=float(row["ingestion_time"] or 0.0),
            lateness_seconds=float(row["lateness_seconds"] or 0.0),
            token_id=row["token_id"] or "",
            metadata=metadata,
            recorded_at=float(row["recorded_at"] or 0.0),
        )

    @staticmethod
    def _row_to_correction(row: sqlite3.Row) -> Correction:
        """Convert a ``corrections`` row into a ``Correction`` dataclass.

        Defensive on JSON deserialisation — a corrupt ``old_value`` /
        ``new_value`` / ``metadata`` cell falls back to a string /
        empty dict rather than raising.
        """
        try:
            old_value: Any = json.loads(row["old_value"] or "null")
        except Exception:
            old_value = row["old_value"]
        try:
            new_value: Any = json.loads(row["new_value"] or "null")
        except Exception:
            new_value = row["new_value"]
        try:
            metadata = json.loads(row["metadata"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        return Correction(
            correction_id=row["correction_id"],
            observation_id=row["observation_id"] or "",
            source=row["source"] or "",
            source_id=row["source_id"] or "",
            field_path=row["field_path"],
            old_value=old_value,
            new_value=new_value,
            reason=row["reason"],
            actor=row["actor"] or "system",
            metadata=metadata,
            corrected_at=float(row["corrected_at"] or 0.0),
        )

    def close(self) -> None:
        """No-op (the handler opens a per-call connection, so there's
        no long-lived connection to close). Kept for API symmetry with
        ``raw_vault.close`` / ``clob_client.close`` so a caller that
        loops over every connector + storage in a shutdown list
        doesn't crash on the handler.
        """
        return None


# ── Module-level singleton ────────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion / observability
# module (``raw_vault``, ``dead_letter_queue``, ``lineage_tracker`` …).
# Importers grab it at module-import time; the constructor allocates the
# SQLite db (creating the parent dir + tables if absent) — no network / I/O
# beyond the local SQLite file.
#
# Defensive: if construction fails (e.g. the SQLite file is on a
# read-only path AND the /tmp fallback also fails — extremely rare),
# ``late_data_handler`` is set to ``None`` so the pipeline's
# best-effort wiring no-ops rather than crashing every ``process``
# call (mirrors the ``lineage_tracker`` defensive convention).
try:
    late_data_handler = LateDataHandler()
except Exception as e:  # noqa: BLE001 — defensive: must never block import
    logger.warning(
        "[late_data] singleton construction failed: %s — late-data "
        "recording will be a no-op",
        e,
    )
    late_data_handler = None  # type: ignore[assignment]


__all__ = [
    "LateDataHandler",
    "LateArrival",
    "Correction",
    "late_data_handler",
    "LATE_THRESHOLD_S",
    "LATE_RATE_ALERT_THRESHOLD",
    "CORRECTION_REASONS",
    "DEFAULT_DB_PATH",
]
