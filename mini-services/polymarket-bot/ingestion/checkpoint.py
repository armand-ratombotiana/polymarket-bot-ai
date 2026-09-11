"""Checkpoint system — enables resume after crash/restart.

For each data source:
1. Tracks the last successfully processed record
2. On restart, resumes from the last checkpoint
3. Supports both timestamp-based and sequence-based checkpoints
4. Handles offset tracking for paginated APIs

This ensures:
- No data is missed during downtime
- No duplicates are created on restart
- Processing can be resumed from any point

W31-4 — Checkpoint manager for the ingestion pipeline.

The manager is SQLite-backed (a dedicated db file at
``CHECKPOINT_DB_PATH``, defaulting to ``/app/data/checkpoints.db``)
so checkpoints survive process restarts. Mirrors the persistence
convention of ``core.alerting.AlertEngine`` and
``ingestion.dead_letter.DeadLetterQueue`` — a dedicated SQLite file
so an ingestion checkpoint write can never perturb the audit /
decision-ledger / observability stores.

Three flavours of checkpoint are supported:

  * **timestamp-based** (default) — ``last_processed`` is a Unix
    epoch float. Used by sources that emit records with monotonic
    timestamps (e.g. WebSocket trade feeds). On restart, the
    ingestion pipeline asks for records with
    ``timestamp > last_processed``.
  * **sequence-based** — ``last_processed`` is an integer sequence
    number. Used by sources that emit records with sequential IDs
    (e.g. ``trade_id`` from the Polymarket CLOB REST API). On
    restart, the pipeline asks for records with
    ``sequence_id > last_processed``.
  * **offset-based** (paginated APIs) — ``offset`` is the next page
    cursor. Used by sources that expose paginated listing endpoints
    (e.g. ``GET /markets?offset=N``). On restart, the pipeline
    resumes pagination from ``offset``.

Contract
--------
``save(source, last_processed=None, ...) -> bool``
    Save (or update) a checkpoint. ``None`` arguments preserve the
    existing value so callers can update a single field (e.g. just
    the offset) without re-supplying the rest. Returns ``True`` on
    success, ``False`` on storage error (logged + swallowed).

``load(source) -> Checkpoint | None``
    Load the latest checkpoint for a source. Returns ``None`` if
    the source has no checkpoint yet (i.e. first run).

``resume(source) -> Checkpoint | None``
    Alias for ``load`` — semantically named for the restart-time
    call site (``checkpoint = manager.resume("clob_rest")``).

``list_checkpoints() -> list[Checkpoint]``
    List every checkpoint (alphabetically by source). Used by the
    ``GET /api/ingestion/checkpoints`` admin endpoint.

``clear(source=None) -> int``
    Drop one source's checkpoint (or every source's). Used by the
    ``POST /api/ingestion/checkpoints/clear`` admin endpoint.

Thread-safety
-------------
Every public method opens its own ``sqlite3.connect(self._db_path)``
context manager. SQLite serializes writes via file-level locking, so
concurrent writes from two threads / processes are safe — the second
writer blocks until the first commits. Reads use WAL journal mode
(set once at init time) so readers don't block writers.

Resume semantics
----------------
``load(source)`` returns the LAST checkpoint that was successfully
committed. The ingestion pipeline MUST checkpoint only AFTER the
record has been durably stored (e.g. ``record_snapshot`` /
``record_trade`` has returned) so a crash between processing and
checkpoint doesn't lose data. The next restart re-processes the
last record (idempotent via the dedup registry, so a duplicate is
caught at the dedup layer).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_DB_PATH = Path(
    os.environ.get("CHECKPOINT_DB_PATH", "/app/data/checkpoints.db")
)


@dataclass
class Checkpoint:
    """A single source's last-processed position.

    Attributes:
        source: Source identifier (e.g. ``"clob_rest"``,
            ``"gamma_api"``, ``"ws_book"``). Mirrors the
            ``source_id`` convention in ``core/ingestion/source_registry``.
        last_processed: The last successfully processed record's
            position. Float for timestamp-based checkpoints, int for
            sequence-based. ``0.0`` means "no records processed yet".
        last_processed_type: ``"timestamp"`` (default) / ``"sequence"``
            / ``"offset"`` / ``"cursor"`` — how to interpret
            ``last_processed``. The restart path branches on this
            value.
        last_processed_at: Wall-clock time of the last successful
            processing. Used by the health monitor's
            ``no_data_received`` alert.
        offset: For paginated APIs — the next page cursor (default
            0 = first page). Independent of ``last_processed`` so a
            source can track both (e.g. ``last_processed`` = the
            timestamp of the most recent record seen on the current
            page; ``offset`` = the page number).
        metadata: Additional source-specific state — e.g. the
            ``cursor_token`` for cursor-based pagination, the
            ``batch_id`` for batch-processing pipelines, the
            ``ws_subscriber_id`` for resumable WebSocket sessions.
        created_at: When this checkpoint was first written.
        updated_at: When this checkpoint was last updated.
    """

    source: str
    last_processed: float = 0.0
    last_processed_type: str = "timestamp"
    last_processed_at: float = 0.0
    offset: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


class CheckpointManager:
    """SQLite-backed checkpoint manager.

    The manager keeps ONE row per source (primary key on
    ``source``). ``save`` is an UPSERT — it inserts a new row if
    none exists, otherwise updates the existing row. ``None``
    arguments preserve the existing value so callers can update
    individual fields without re-supplying the rest.

    All persistence is fire-and-forget from the caller's perspective:
    storage errors are logged at ERROR level and swallowed so an
    ingestion checkpoint write can never break the upstream data
    flow.
    """

    def __init__(self, db_path: Path = CHECKPOINT_DB_PATH) -> None:
        self._db_path = db_path
        self._init_db()

    # ── Schema setup ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the checkpoints table if it doesn't exist.

        Idempotent so repeated ``CheckpointManager()`` constructions
        against the same db file are safe. Parent directory is
        auto-created so a fresh sandbox with no ``/app/data``
        directory works (mirrors ``core.alerting.AlertEngine._init_db``).
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    # WAL may be unavailable on certain filesystems
                    # (e.g. network mounts). Fall back to the default
                    # rollback journal silently.
                    pass
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        source                TEXT PRIMARY KEY,
                        last_processed        REAL NOT NULL DEFAULT 0,
                        last_processed_type   TEXT NOT NULL DEFAULT 'timestamp',
                        last_processed_at     REAL NOT NULL DEFAULT 0,
                        offset                INTEGER NOT NULL DEFAULT 0,
                        metadata              TEXT DEFAULT '{}',
                        created_at            REAL NOT NULL,
                        updated_at            REAL NOT NULL
                    )
                    """
                )
        except Exception as e:  # noqa: BLE001 — storage must not break callers
            logger.error(
                "[checkpoint] _init_db failed (path=%s): %s",
                self._db_path,
                e,
            )

    # ── Public API ─────────────────────────────────────────────────────────

    def save(
        self,
        source: str,
        last_processed: float | int | None = None,
        last_processed_type: str | None = None,
        last_processed_at: float | None = None,
        offset: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Save (or update) a checkpoint for ``source``.

        This is an UPSERT — inserts a new row if the source has no
        checkpoint, otherwise updates the existing row. ``None``
        arguments preserve the existing value (or default to ``0`` /
        ``"timestamp"`` / ``{}`` for a brand-new row).

        Args:
            source: Source identifier.
            last_processed: Last successfully processed record's
                position. Float for timestamps, int for sequences.
            last_processed_type: ``"timestamp"`` / ``"sequence"`` /
                ``"offset"`` / ``"cursor"``.
            last_processed_at: Wall-clock time of the last
                successful processing. Defaults to ``time.time()``
                if ``None`` AND no prior checkpoint exists; otherwise
                defaults to the prior value.
            offset: For paginated APIs — next page cursor.
            metadata: Additional source-specific state. Merged with
                the existing metadata (caller's keys win on
                collision).

        Returns:
            ``True`` on success, ``False`` on storage error.
        """
        try:
            now = time.time()
            with sqlite3.connect(self._db_path) as conn:
                existing = conn.execute(
                    "SELECT last_processed, last_processed_type, "
                    "last_processed_at, offset, metadata, created_at "
                    "FROM checkpoints WHERE source = ?",
                    (source,),
                ).fetchone()
                if existing:
                    (
                        cur_last,
                        cur_type,
                        cur_last_at,
                        cur_offset,
                        cur_meta_json,
                        created_at,
                    ) = existing
                    try:
                        cur_meta = json.loads(cur_meta_json or "{}")
                        if not isinstance(cur_meta, dict):
                            cur_meta = {}
                    except Exception:
                        cur_meta = {}
                    new_last = (
                        last_processed
                        if last_processed is not None
                        else float(cur_last or 0.0)
                    )
                    new_type = last_processed_type or cur_type or "timestamp"
                    new_last_at = (
                        last_processed_at
                        if last_processed_at is not None
                        else float(cur_last_at or 0.0)
                    )
                    new_offset = (
                        offset if offset is not None else int(cur_offset or 0)
                    )
                    merged_meta = {**cur_meta, **(metadata or {})}
                else:
                    created_at = now
                    new_last = last_processed if last_processed is not None else 0.0
                    new_type = last_processed_type or "timestamp"
                    new_last_at = (
                        last_processed_at
                        if last_processed_at is not None
                        else now
                    )
                    new_offset = offset if offset is not None else 0
                    merged_meta = metadata or {}

                conn.execute(
                    """
                    INSERT OR REPLACE INTO checkpoints
                    (source, last_processed, last_processed_type,
                     last_processed_at, offset, metadata, created_at,
                     updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        float(new_last),
                        new_type,
                        float(new_last_at),
                        int(new_offset),
                        json.dumps(merged_meta, default=str, sort_keys=True),
                        float(created_at),
                        float(now),
                    ),
                )
            return True
        except Exception as e:  # noqa: BLE001 — never break the caller
            logger.error(
                "[checkpoint] save failed (source=%s): %s",
                source,
                e,
            )
            return False

    def load(self, source: str) -> Checkpoint | None:
        """Load the latest checkpoint for ``source``.

        Returns ``None`` if the source has no checkpoint yet (i.e.
        first run).
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM checkpoints WHERE source = ?",
                    (source,),
                ).fetchone()
                if not row:
                    return None
                return self._row_to_checkpoint(row)
        except Exception as e:  # noqa: BLE001
            logger.error("[checkpoint] load failed: %s", e)
            return None

    def resume(self, source: str) -> Checkpoint | None:
        """Alias for ``load`` — semantically named for the restart-time call site.

        The canonical caller pattern:

        .. code-block:: python

            cp = checkpoint_manager.resume("clob_rest")
            if cp is None:
                # First run — start from the beginning.
                last_ts = 0.0
            else:
                last_ts = cp.last_processed
            for record in fetch_records(since=last_ts):
                process(record)
                checkpoint_manager.save("clob_rest", last_processed=record.timestamp)
        """
        return self.load(source)

    def list_checkpoints(self) -> list[Checkpoint]:
        """List every checkpoint (alphabetically by source)."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM checkpoints ORDER BY source ASC"
                ).fetchall()
                return [self._row_to_checkpoint(r) for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.error("[checkpoint] list_checkpoints failed: %s", e)
            return []

    def clear(self, source: str | None = None) -> int:
        """Drop one source's checkpoint (or every source's).

        Args:
            source: When supplied, clears ONLY that source's
                checkpoint. When ``None``, clears every source.

        Returns:
            Number of checkpoints deleted.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                if source:
                    cur = conn.execute(
                        "DELETE FROM checkpoints WHERE source = ?",
                        (source,),
                    )
                else:
                    cur = conn.execute("DELETE FROM checkpoints")
                return int(cur.rowcount or 0)
        except Exception as e:  # noqa: BLE001
            logger.error("[checkpoint] clear failed: %s", e)
            return 0

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
        """Convert a sqlite3.Row into a ``Checkpoint`` dataclass."""
        try:
            metadata = json.loads(row["metadata"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        return Checkpoint(
            source=row["source"],
            last_processed=float(row["last_processed"] or 0.0),
            last_processed_type=row["last_processed_type"] or "timestamp",
            last_processed_at=float(row["last_processed_at"] or 0.0),
            offset=int(row["offset"] or 0),
            metadata=metadata,
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )


# ── Module-level singleton ─────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion / observability
# module. Importers grab it at module-import time; the constructor
# allocates the SQLite db (creating the parent dir + table if absent).
checkpoint_manager = CheckpointManager()


__all__ = [
    "Checkpoint",
    "CheckpointManager",
    "checkpoint_manager",
    "CHECKPOINT_DB_PATH",
]
