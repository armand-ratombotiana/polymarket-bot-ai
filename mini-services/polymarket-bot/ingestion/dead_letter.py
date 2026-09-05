"""Dead-letter queue — stores records that failed processing.

When a record fails validation, normalization, or storage:
1. It's moved to the dead-letter queue
2. The error reason is recorded
3. The original payload is preserved
4. An alert is fired
5. The record can be retried later

This ensures no data is lost — even invalid records are preserved
for debugging and potential reprocessing after fixes.

W31-4 — Dead-letter queue for the ingestion pipeline.

The queue is SQLite-backed (a dedicated db file at ``DLQ_DB_PATH``,
defaulting to ``/app/data/dead_letter.db``) so records survive
process restarts. Mirrors the persistence convention of
``core.alerting.AlertEngine`` (a dedicated SQLite file separate from
the audit / decision-ledger / observability stores so an ingestion
hiccup can never perturb the audit trail).

Contract
--------
``add(source, record_type, payload, reason, error, ...) -> str``
    Adds a record to the queue. Returns the new ``record_id`` (UUID4
    hex) — or empty string on failure (the underlying SQLite writes
    are wrapped in try/except so an I/O hiccup never breaks the
    caller; the error is logged at ERROR level).

``get(record_id) -> DeadLetterRecord | None``
    Retrieves a single record by id. Returns ``None`` if not found.

``get_pending(limit=100, source=None) -> list[DeadLetterRecord]``
    Returns pending records (oldest first), optionally filtered by
    source. Used by the retry driver to find records eligible for
    reprocessing.

``mark_retried(record_id, success=True) -> bool``
    Increments the retry count. On success, status becomes ``retried``;
    on failure after ``MAX_RETRIES`` attempts, status becomes
    ``abandoned``. Returns ``False`` if the record doesn't exist.

``clear(source=None, status=None) -> int``
    Drops records from the queue. With no args, drops everything.
    Returns the number of records deleted. Used by the admin
    ``POST /api/ingestion/dlq/clear`` endpoint (W31-4 wiring).

``depth(status=None) -> int``
    Returns the queue depth (optionally filtered by status). Used by
    the health monitor to fire the ``dlq_depth_high`` alert when depth
    exceeds ``ALERT_THRESHOLD`` (default 100 records).

``get_stats() -> dict``
    Returns a JSON-serializable summary — total / pending / retried /
    abandoned counts plus a per-source breakdown. Exposed via the
    ``GET /api/ingestion/dlq/stats`` endpoint (W31-4 wiring).

Alerts
------
On each ``add()`` call, the queue fires a ``dead_letter_record_added``
alert via ``core.alerting.alert_engine.record_alert`` (lazy import so
the queue can be imported in environments where the alert engine is
not yet ready). The alert is WARNING severity, ``category="data"``.
Callers that want to suppress the alert (e.g. tests) can pass
``alert_enabled=False`` to the constructor.

Retry policy
------------
``MAX_RETRIES = 3`` (default). After 3 failed retry attempts, the
record's status flips to ``abandoned`` so the retry driver stops
picking it up. The record remains in the queue (for forensic audit)
until ``clear()`` is called explicitly.

Thread-safety
-------------
Every public method opens its own ``sqlite3.connect(self._db_path)``
context manager. SQLite serializes writes via file-level locking, so
concurrent writes from two threads / processes are safe — the second
writer blocks until the first commits. Reads use ``PRAGMA
journal_mode=WAL`` (set once at init time) so readers don't block
writers.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DLQ_DB_PATH = Path(os.environ.get("DLQ_DB_PATH", "/app/data/dead_letter.db"))


@dataclass
class DeadLetterRecord:
    """A single record moved to the dead-letter queue.

    Attributes:
        record_id: UUID4 hex — primary key.
        source: Source identifier (e.g. ``"clob_rest"``,
            ``"gamma_api"``, ``"ws_book"``). Mirrors the
            ``source_id`` convention in ``core/ingestion/source_registry``.
        record_type: ``"snapshot"`` / ``"trade"`` / ``"fill"`` /
            ``"event"`` — the record's type so the retry driver can
            dispatch to the correct reprocessing path.
        payload: The original payload, preserved verbatim. JSON-loads
            on retrieval so the caller sees the same dict the producer
            submitted (best-effort — non-JSON values are stored as
            ``{"raw": str(value)}``).
        reason: High-level reason category. Default values:
            ``"validation_failed"``, ``"storage_error"``,
            ``"normalization_error"``, ``"schema_mismatch"``,
            ``"unknown"``.
        error: Detailed error message (typically ``str(exception)``).
        stack_trace: Optional stack trace for debugging (defaults to
            empty string).
        first_seen: Unix timestamp when the record first entered the
            DLQ.
        last_attempt: Unix timestamp of the last retry attempt (0.0
            if never retried).
        retry_count: Number of retry attempts so far.
        status: ``"pending"`` (default — eligible for retry),
            ``"retried"`` (successfully reprocessed — kept for
            audit), ``"abandoned"`` (exceeded MAX_RETRIES — parked
            permanently until manual intervention).
        metadata: Additional context — e.g. the validator's
            ``warnings`` list, the original request URL, the
            processing pipeline stage.
    """

    record_id: str
    source: str
    record_type: str
    payload: dict[str, Any]
    reason: str
    error: str
    stack_trace: str = ""
    first_seen: float = 0.0
    last_attempt: float = 0.0
    retry_count: int = 0
    status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)


class DeadLetterQueue:
    """SQLite-backed dead-letter queue.

    The queue is append-mostly: records enter via ``add()`` and leave
    via ``mark_retried(success=True)`` (which keeps the row but flips
    the status) or ``clear()`` (which deletes the row). The
    ``retry_count`` / ``last_attempt`` / ``status`` columns are
    updated in place by ``mark_retried`` so the retry driver's
    progress is durable across restarts.

    All persistence is fire-and-forget from the caller's perspective:
    storage errors are logged at ERROR level and swallowed so an
    ingestion pipeline hiccup (e.g. a transient disk-full condition)
    can never break the upstream data flow.
    """

    #: Maximum retry attempts before a record is marked ``abandoned``.
    MAX_RETRIES: int = 3

    #: Queue depth threshold — the health monitor fires a CRITICAL
    #: ``dlq_depth_high`` alert when depth exceeds this value.
    ALERT_THRESHOLD: int = 100

    def __init__(
        self,
        db_path: Path = DLQ_DB_PATH,
        alert_enabled: bool = True,
        max_retries: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._alert_enabled = alert_enabled
        if max_retries is not None:
            self.MAX_RETRIES = max_retries
        self._init_db()

    # ── Schema setup ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the dead_letter table + indexes if they don't exist.

        Idempotent so repeated ``DeadLetterQueue()`` constructions
        against the same db file are safe. Parent directory is
        auto-created so a fresh sandbox with no ``/app/data`` directory
        works (mirrors ``core.alerting.AlertEngine._init_db``).
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                # WAL journal mode so concurrent readers don't block
                # the writer (the retry driver polls ``get_pending``
                # while the ingestion pipeline is appending via ``add``).
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    # WAL may be unavailable on certain filesystems
                    # (e.g. network mounts). Fall back to the default
                    # rollback journal silently — the queue still
                    # works, just with reader/writer contention.
                    pass
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS dead_letter (
                        record_id     TEXT PRIMARY KEY,
                        source        TEXT NOT NULL,
                        record_type   TEXT NOT NULL,
                        payload       TEXT NOT NULL,
                        reason        TEXT NOT NULL,
                        error         TEXT NOT NULL,
                        stack_trace   TEXT DEFAULT '',
                        first_seen    REAL NOT NULL,
                        last_attempt  REAL DEFAULT 0,
                        retry_count   INTEGER DEFAULT 0,
                        status        TEXT DEFAULT 'pending',
                        metadata      TEXT DEFAULT '{}'
                    )
                    """
                )
                # ``(status, first_seen ASC)`` — the retry driver's
                # ``get_pending`` query filters on ``status = 'pending'``
                # and orders by ``first_seen ASC`` (oldest first) so a
                # full scan would otherwise be required to surface the
                # oldest pending records.
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_dlq_status_ts
                    ON dead_letter(status, first_seen ASC)
                    """
                )
                # ``(source)`` — the per-source ``get_pending(source=X)``
                # query and the ``get_stats`` per-source breakdown.
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_dlq_source
                    ON dead_letter(source)
                    """
                )
        except Exception as e:  # noqa: BLE001 — storage must not break callers
            logger.error("[dead_letter] _init_db failed (path=%s): %s", self._db_path, e)

    # ── Public API ─────────────────────────────────────────────────────────

    def add(
        self,
        source: str,
        record_type: str,
        payload: dict[str, Any],
        reason: str,
        error: str,
        stack_trace: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Add a record to the dead-letter queue.

        Args:
            source: Source identifier (e.g. ``"clob_rest"``).
            record_type: ``"snapshot"`` / ``"trade"`` / ``"fill"`` /
                ``"event"``.
            payload: The original payload dict. JSON-serialised on
                storage (``default=str`` so non-JSON values like
                ``datetime`` are still accepted).
            reason: High-level reason (e.g.
                ``"validation_failed"``, ``"storage_error"``).
            error: Detailed error message (typically
                ``str(exception)``).
            stack_trace: Optional stack trace for debugging.
            metadata: Optional context dict.

        Returns:
            The new ``record_id`` (UUID4 hex) — or empty string on
            failure (the underlying SQLite write is wrapped in
            try/except so an I/O hiccup never breaks the caller).
        """
        record_id = uuid.uuid4().hex
        now = time.time()
        meta_json = json.dumps(metadata or {}, default=str, sort_keys=True)
        payload_json = json.dumps(payload, default=str, sort_keys=True)
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO dead_letter
                    (record_id, source, record_type, payload, reason,
                     error, stack_trace, first_seen, last_attempt,
                     retry_count, status, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'pending', ?)
                    """,
                    (
                        record_id,
                        source,
                        record_type,
                        payload_json,
                        reason,
                        error,
                        stack_trace,
                        now,
                        meta_json,
                    ),
                )
        except Exception as e:  # noqa: BLE001 — never break the caller
            logger.error(
                "[dead_letter] add failed (source=%s reason=%s): %s",
                source,
                reason,
                e,
            )
            return ""
        logger.warning(
            "[dead_letter] record added: source=%s reason=%s type=%s record_id=%s",
            source,
            reason,
            record_type,
            record_id,
        )
        self._fire_alert(source, reason, error, record_id)
        return record_id

    def get(self, record_id: str) -> DeadLetterRecord | None:
        """Retrieve a single dead-letter record by id.

        Returns ``None`` if not found.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM dead_letter WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if not row:
                    return None
                return self._row_to_record(row)
        except Exception as e:  # noqa: BLE001 — never break the caller
            logger.error("[dead_letter] get failed: %s", e)
            return None

    def get_pending(
        self,
        limit: int = 100,
        source: str | None = None,
    ) -> list[DeadLetterRecord]:
        """Return pending records (oldest first), optionally filtered.

        Args:
            limit: Maximum records to return. Default 100.
            source: Optional source filter. When ``None``, returns
                pending records from every source.

        Returns:
            List of ``DeadLetterRecord`` (empty list on failure).
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                if source:
                    rows = conn.execute(
                        """
                        SELECT * FROM dead_letter
                        WHERE status = 'pending' AND source = ?
                        ORDER BY first_seen ASC
                        LIMIT ?
                        """,
                        (source, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM dead_letter
                        WHERE status = 'pending'
                        ORDER BY first_seen ASC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                return [self._row_to_record(r) for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.error("[dead_letter] get_pending failed: %s", e)
            return []

    def mark_retried(self, record_id: str, success: bool = True) -> bool:
        """Mark a record as retried.

        Increments ``retry_count`` and updates ``last_attempt``. On
        success, status becomes ``"retried"`` (kept for audit). On
        failure, status becomes ``"abandoned"`` if the retry count
        exceeds ``MAX_RETRIES``; otherwise remains ``"pending"`` so
        the retry driver picks it up again on the next pass.

        Args:
            record_id: The record's UUID4 hex.
            success: Whether the retry succeeded.

        Returns:
            ``True`` if the record was found and updated, ``False``
            otherwise (record not found or storage error).
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT retry_count FROM dead_letter WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if not row:
                    return False
                new_count = int(row[0] or 0) + 1
                if success:
                    new_status = "retried"
                elif new_count >= self.MAX_RETRIES:
                    new_status = "abandoned"
                else:
                    new_status = "pending"
                conn.execute(
                    """
                    UPDATE dead_letter
                    SET retry_count = ?, last_attempt = ?, status = ?
                    WHERE record_id = ?
                    """,
                    (new_count, time.time(), new_status, record_id),
                )
                return True
        except Exception as e:  # noqa: BLE001
            logger.error("[dead_letter] mark_retried failed: %s", e)
            return False

    def clear(
        self,
        source: str | None = None,
        status: str | None = None,
    ) -> int:
        """Drop records from the queue.

        Args:
            source: When supplied, only records from this source are
                deleted. When ``None``, records from every source are
                deleted.
            status: When supplied, only records with this status are
                deleted (e.g. ``"abandoned"`` to clean up parked
                records). When ``None``, records of any status are
                deleted.

        Returns:
            Number of records deleted.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                if source and status:
                    cur = conn.execute(
                        "DELETE FROM dead_letter WHERE source = ? AND status = ?",
                        (source, status),
                    )
                elif source:
                    cur = conn.execute(
                        "DELETE FROM dead_letter WHERE source = ?",
                        (source,),
                    )
                elif status:
                    cur = conn.execute(
                        "DELETE FROM dead_letter WHERE status = ?",
                        (status,),
                    )
                else:
                    cur = conn.execute("DELETE FROM dead_letter")
                return int(cur.rowcount or 0)
        except Exception as e:  # noqa: BLE001
            logger.error("[dead_letter] clear failed: %s", e)
            return 0

    def depth(self, status: str | None = None) -> int:
        """Return the queue depth (optionally filtered by status)."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                if status:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM dead_letter WHERE status = ?",
                        (status,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM dead_letter"
                    ).fetchone()
                return int(row[0]) if row else 0
        except Exception as e:  # noqa: BLE001
            logger.error("[dead_letter] depth failed: %s", e)
            return 0

    def get_stats(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of the queue state.

        Returns a dict with keys: ``total``, ``pending``,
        ``retried``, ``abandoned``, ``by_source`` (dict of
        source -> count for ALL statuses). The shape is stable so
        a dashboard polling ``GET /api/ingestion/dlq/stats`` doesn't
        need to handle missing keys.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM dead_letter"
                ).fetchone()[0]
                pending = conn.execute(
                    "SELECT COUNT(*) FROM dead_letter WHERE status = 'pending'"
                ).fetchone()[0]
                retried = conn.execute(
                    "SELECT COUNT(*) FROM dead_letter WHERE status = 'retried'"
                ).fetchone()[0]
                abandoned = conn.execute(
                    "SELECT COUNT(*) FROM dead_letter WHERE status = 'abandoned'"
                ).fetchone()[0]
                by_source_rows = conn.execute(
                    "SELECT source, COUNT(*) AS cnt FROM dead_letter GROUP BY source"
                ).fetchall()
                by_source = {row[0]: int(row[1]) for row in by_source_rows}
                return {
                    "total": int(total),
                    "pending": int(pending),
                    "retried": int(retried),
                    "abandoned": int(abandoned),
                    "by_source": by_source,
                }
        except Exception as e:  # noqa: BLE001
            logger.error("[dead_letter] get_stats failed: %s", e)
            return {
                "total": 0,
                "pending": 0,
                "retried": 0,
                "abandoned": 0,
                "by_source": {},
            }

    # ── Alerting ────────────────────────────────────────────────────────────

    def _fire_alert(
        self,
        source: str,
        reason: str,
        error: str,
        record_id: str,
    ) -> None:
        """Fire a WARNING alert via ``core.alerting.alert_engine``.

        Lazy-imports ``core.alerting`` so the queue can be imported
        in environments where the alert engine is not yet ready
        (e.g. unit tests that don't want alert persistence). The
        alert is fire-and-forget — any exception is swallowed so an
        alerting hiccup can never break the dead-letter recording
        path.
        """
        if not self._alert_enabled:
            return
        try:
            from core.alerting import SEVERITY_WARNING, alert_engine

            alert_engine.record_alert(
                name="dead_letter_record_added",
                category="data",
                severity=SEVERITY_WARNING,
                message=(
                    f"Dead-letter record added (source={source} "
                    f"reason={reason}): {error[:200]}"
                ),
                metadata={
                    "source": source,
                    "reason": reason,
                    "record_id": record_id,
                },
            )
        except Exception as e:  # noqa: BLE001 — alerting must never break callers
            logger.debug(
                "[dead_letter] alert fire failed (continuing): %s", e
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DeadLetterRecord:
        """Convert a sqlite3.Row into a ``DeadLetterRecord`` dataclass.

        Defensive on JSON deserialisation — a corrupt ``payload`` /
        ``metadata`` cell (e.g. truncated by a disk-full condition)
        falls back to ``{"raw": "<cell>"}`` / ``{}`` rather than
        raising.
        """
        try:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                payload = {"raw": payload}
        except Exception:
            payload = {"raw": row["payload"]}
        try:
            metadata = json.loads(row["metadata"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        return DeadLetterRecord(
            record_id=row["record_id"],
            source=row["source"],
            record_type=row["record_type"],
            payload=payload,
            reason=row["reason"],
            error=row["error"],
            stack_trace=row["stack_trace"] or "",
            first_seen=float(row["first_seen"] or 0.0),
            last_attempt=float(row["last_attempt"] or 0.0),
            retry_count=int(row["retry_count"] or 0),
            status=row["status"] or "pending",
            metadata=metadata,
        )


# ── Module-level singleton ─────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion / observability
# module (``core.alerting.alert_engine``, ``core.dedup.dedup_registry`` …).
# Importers grab it at module-import time; the constructor allocates the
# SQLite db (creating the parent dir + table if absent) — no network / I/O
# beyond the local SQLite file.
dead_letter_queue = DeadLetterQueue()


__all__ = [
    "DeadLetterQueue",
    "DeadLetterRecord",
    "dead_letter_queue",
    "DLQ_DB_PATH",
]
