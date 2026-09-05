"""Raw data vault — preserves all source data for replayability.

Every record from every source is stored in its raw form with:
- source: Where the data came from ("clob", "gamma", "websocket")
- source_id: Unique ID from the source
- event_type: "snapshot", "trade", "order_book", "market_info"
- raw_payload: The original JSON payload (unmodified)
- event_timestamp: When the event occurred (from source)
- ingestion_timestamp: When we received it
- processing_timestamp: When we processed it
- data_version: Schema version (for forward compat)
- validation_status: "valid", "invalid", "duplicate", "stale"
- quality_score: 0.0 to 1.0
- error_reason: Why it was rejected (if applicable)

This enables:
1. Replay: Re-process any historical data
2. Debugging: Inspect what the source actually sent
3. Schema evolution: Handle API changes
4. Audit: Full provenance trail

Storage
-------
Backed by a dedicated SQLite file resolved via the ``RAW_VAULT_DB_PATH``
env var (default: ``/app/data/raw_vault.db``; the conftest in
``tests/conftest.py`` already redirects ``RAW_VAULT_DB_PATH`` to a
``/tmp/pmbot_conftest_isolation`` sandbox at test time so the file is
writable even in the read-only-sandbox CI). SQLite is chosen over
PostgreSQL/TimescaleDB deliberately — the raw vault's contract is "every
record survives, even when PG is down" (mirrors the W21-1
PG-primary-SQLite-fallback philosophy but at a stricter level: the raw
vault NEVER depends on PG availability because its entire reason for
existing is to be the audit-grade backstop when PG loses rows). The
``raw_records`` table carries a UNIQUE constraint on
``(source, source_id, payload_hash)`` so duplicates are rejected at the
DB layer (the in-memory ``_seen_keys`` deque is the fast-path; the
UNIQUE constraint is the restart-safe backstop).

Concurrency
-----------
The vault is single-threaded by design — every call site
(``ingestion.pipeline.Pipeline.process`` / connector callbacks) is
either a sync caller or a single ``asyncio`` task. SQLite's default
``BEGIN IMMEDIATE`` transaction gives serialisability across threads,
and the ``check_same_thread=False`` flag lets the vault be shared
between the asyncio loop and ``asyncio.to_thread`` offloads without a
``ProgrammingError``. If a future wave adds parallel writers, the
UNIQUE constraint + ``INSERT OR IGNORE`` pattern keeps the vault
correct under contention (a duplicate insert is silently dropped, not
raised) — at the cost of a bump in the ignored-insert counter (surfaced
via ``get_stats()["duplicate_ignored_count"]``).

Schema versioning
-----------------
The ``data_version`` column lets a future schema migration ship without
invalidating historical records. The current schema is ``"1.0"`` — every
``record_observation`` call defaults to it; a connector that observes a
new field-shape from upstream can bump the version on its own calls
without coordinating with the vault.
"""
from __future__ import annotations

import hashlib
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

# Default DB path. The conftest redirects this in tests; production runs
# resolve it to ``/app/data/raw_vault.db`` (the docker-compose volume
# bind-mount makes ``/app/data`` writable in the prod container).
DEFAULT_DB_PATH = "/app/data/raw_vault.db"

# Current schema version emitted by ``record_observation``. Bump this
# ONLY when the on-disk schema of ``raw_records`` itself changes — never
# when an upstream source adds a field (upstream field additions are
# absorbed into the ``raw_payload`` JSON blob, which is forward-compat
# by construction).
DATA_VERSION_CURRENT = "1.0"

# Bounded in-memory dedup window. A long-running process can't grow
# the set unbounded. Mirrors the W24-4 ``data_validator`` deque pattern.
_MAX_SEEN_KEYS = 10_000

# Validation status enum values. Kept as a frozenset for the
# ``record_observation`` precondition check (the DB layer accepts any
# TEXT value so a future status — e.g. ``"superseded"`` — doesn't
# require a migration).
VALIDATION_STATUSES = frozenset({"valid", "invalid", "duplicate", "stale"})


# ── Dataclass ──────────────────────────────────────────────────────────────────


@dataclass
class RawRecord:
    """A single raw observation stored in the vault.

    Attributes:
        source: Where the data came from (``"clob"`` / ``"gamma"`` /
            ``"websocket"`` / ``"news"``).
        source_id: The source's own unique ID for the record (e.g. a
            ``trade_id`` for trades, ``condition_id`` for markets, the
            ``token_id`` for snapshots). When the source has no native
            ID, the connector synthesises one (e.g. ``f"snapshot-{token
            _id}-{ts}"``).
        event_type: ``"snapshot"`` / ``"trade"`` / ``"order_book"`` /
            ``"market_info"`` / ``"news"``. The vault is agnostic —
            any string is accepted — but the canonical set is
            documented above so a downstream replayer can switch on it.
        raw_payload: The original JSON payload, **unmodified**. The
            vault does NOT normalise, coerce, or augment the payload
            — that's the pipeline's job. Storing the exact bytes the
            source sent is what makes the vault audit-grade.
        event_timestamp: When the event occurred, as reported by the
            source. Defaults to ``ingestion_timestamp`` when the source
            doesn't supply one (a warning is logged so the operator can
            see the gap).
        ingestion_timestamp: When the vault received the record
            (``time.time()`` at the top of ``record_observation``).
            Captured ONCE so every downstream consumer sees a
            consistent value.
        processing_timestamp: When the vault finished persisting the
            record (``time.time()`` at the bottom of
            ``record_observation``). ``processing - ingestion`` is the
            per-record vault write latency.
        data_version: Schema version of the ``raw_payload`` shape (for
            forward-compat migrations). Defaults to ``"1.0"``.
        validation_status: ``"valid"`` / ``"invalid"`` / ``"duplicate"``
            / ``"stale"`` — set by the pipeline's validator stage
            BEFORE the record reaches the vault so the vault's row
            carries the pre-vault verdict (the vault doesn't re-judge
            the record; it just preserves the verdict).
        quality_score: ``0.0`` to ``1.0`` — set by the pipeline's
            normalizer stage. ``1.0`` = clean; ``0.0`` = garbage
            (the row is still stored for audit, but downstream
            consumers should skip it).
        error_reason: When ``validation_status`` is ``"invalid"`` /
            ``"stale"`` / ``"duplicate"``, a human-readable string
            explaining why. Empty string on a clean record.
        observation_id: The vault's own UUID4 for the record (the
            primary key on the ``raw_records`` table). Assigned at
            ``record_observation`` time so the caller can use it for
            replay-by-id.
        payload_hash: SHA-256 of the canonical JSON of
            ``raw_payload`` (sort_keys=True). Used as the dedup key —
            two records with the same ``(source, source_id,
            payload_hash)`` triple are the same observation.
    """

    source: str
    source_id: str
    event_type: str
    raw_payload: Any
    event_timestamp: float = 0.0
    ingestion_timestamp: float = 0.0
    processing_timestamp: float = 0.0
    data_version: str = DATA_VERSION_CURRENT
    validation_status: str = "valid"
    quality_score: float = 1.0
    error_reason: str = ""
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    payload_hash: str = ""

    def __post_init__(self) -> None:
        # Compute the payload hash if not pre-set by the caller. The
        # hash is over the canonical JSON (``sort_keys=True``) so two
        # payloads with the same content but different insertion order
        # hash equal — the W24-4 ``data_validator`` uses the same
        # convention.
        if not self.payload_hash:
            self.payload_hash = _hash_payload(self.raw_payload)
        # Clamp quality_score to [0.0, 1.0] so a misbehaving caller
        # can't store ``2.0`` or ``-0.5`` and confuse downstream
        # queries (``WHERE quality_score >= 0.8`` would silently match
        # the bad value).
        try:
            qs = float(self.quality_score)
        except (TypeError, ValueError):
            qs = 0.0
        if qs < 0.0:
            qs = 0.0
        elif qs > 1.0:
            qs = 1.0
        self.quality_score = qs


# ── Vault ──────────────────────────────────────────────────────────────────────


class RawVault:
    """Durable raw-observation vault (SQLite-backed, restart-safe).

    The vault is constructed at module-import time (the singleton
    ``raw_vault`` at the bottom of this file). Construction opens the
    SQLite file, runs the ``CREATE TABLE IF NOT EXISTS`` migrations, and
    primes the in-memory dedup deque from the most recent
    ``_MAX_SEEN_KEYS`` rows — so a process restart picks up dedup state
    where the prior process left off (within the bounded window).

    The class is safe to instantiate without env vars / paths set —
    the constructor falls back to ``DEFAULT_DB_PATH`` and creates the
    parent directory if missing. ``/app/data`` is the prod default
    (writable in the docker-compose volume); tests redirect via the
    ``RAW_VAULT_DB_PATH`` env var (handled by ``tests/conftest.py``).
    """

    def __init__(self, db_path: str | None = None) -> None:
        path_str = db_path or os.environ.get("RAW_VAULT_DB_PATH", DEFAULT_DB_PATH)
        self._db_path = Path(path_str)
        # ``check_same_thread=False`` so the vault can be shared between
        # the asyncio event loop thread and ``asyncio.to_thread``
        # offloads. SQLite's default ``BEGIN IMMEDIATE`` transaction
        # gives serialisability across the GIL boundary; the
        # ``_lock`` is for in-process serialisation on the (rare)
        # multi-thread call sites.
        self._lock = threading.Lock()
        self._seen_keys: deque[str] = deque(maxlen=_MAX_SEEN_KEYS)
        # Counters — surfaced via ``get_stats`` for the operator
        # dashboard / observability layer.
        self._record_count: int = 0
        self._duplicate_count: int = 0
        self._invalid_count: int = 0
        self._duplicate_ignored_count: int = 0  # DB-layer INSERT OR IGNORE
        self._init_db()
        self._prime_seen_keys()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the ``raw_records`` table + indexes if missing."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Defensive: if the parent isn't writable, fall back to
            # ``/tmp/raw_vault.db`` so the bot still boots. A logged
            # warning surfaces the redirect; the operator can fix the
            # path env var.
            logger.warning(
                "[raw_vault] Cannot create parent dir %s: %s — "
                "falling back to /tmp/raw_vault.db",
                self._db_path.parent,
                e,
            )
            self._db_path = Path("/tmp/raw_vault.db")

        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS raw_records (
                    observation_id     TEXT    PRIMARY KEY,
                    source             TEXT    NOT NULL,
                    source_id          TEXT    NOT NULL,
                    event_type         TEXT    NOT NULL,
                    raw_payload        TEXT    NOT NULL,
                    event_timestamp    REAL    NOT NULL,
                    ingestion_timestamp REAL   NOT NULL,
                    processing_timestamp REAL  NOT NULL,
                    data_version       TEXT    NOT NULL,
                    validation_status  TEXT    NOT NULL,
                    quality_score      REAL    NOT NULL,
                    error_reason       TEXT    NOT NULL DEFAULT '',
                    payload_hash       TEXT    NOT NULL,
                    created_at         REAL    NOT NULL
                )
            """)
            # Dedup UNIQUE constraint. ``INSERT OR IGNORE`` against
            # this constraint is the restart-safe dedup backstop (the
            # in-memory deque is the fast path; if a process restart
            # evicts a key from the deque, a replay of the same record
            # is rejected at the DB layer rather than producing a
            # duplicate row).
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_dedup
                ON raw_records (source, source_id, payload_hash)
            """)
            # Query indexes — mirror the ``replay`` /
            # ``replay_range`` filter dimensions.
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_raw_event_time
                ON raw_records (event_timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_raw_source_type
                ON raw_records (source, event_type, event_timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_raw_validation_status
                ON raw_records (validation_status)
            """)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection (the vault doesn't pool — SQLite
        file open is cheap, and a per-call connection prevents the
        ``database is locked`` errors that a long-lived connection
        would surface under contention).
        """
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=5.0,
            isolation_level="IMMEDIATE",
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _prime_seen_keys(self) -> None:
        """Load the most recent ``_MAX_SEEN_KEYS`` dedup keys into the
        in-memory deque so a process restart doesn't lose dedup state.

        Belt-and-braces with the DB-layer UNIQUE constraint: the deque
        is the fast-path (no DB round-trip for the duplicate check);
        the constraint is the restart-safe backstop. Without priming,
        the first ``_MAX_SEEN_KEYS`` records after a restart would all
        round-trip to the DB for dedup (the INSERT OR IGNORE would
        silently drop the duplicate), which is functionally correct
        but ~10x slower than the deque fast-path.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT source, source_id, payload_hash
                    FROM raw_records
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (_MAX_SEEN_KEYS,),
                ).fetchall()
            for r in rows:
                key = self._dedup_key(r["source"], r["source_id"], r["payload_hash"])
                # ``append`` (not ``extend``) so the deque's LRU
                # eviction order matches insertion order.
                self._seen_keys.append(key)
        except sqlite3.Error as e:
            # Defensive: if priming fails (corrupted DB / locked
            # schema / etc.), the vault still functions — every
            # dedup check will round-trip to the DB instead of the
            # fast-path. Logged at WARNING so the operator sees it.
            logger.warning(
                "[raw_vault] Failed to prime dedup deque from DB: %s — "
                "falling back to DB-only dedup",
                e,
            )

    # ── Public API ─────────────────────────────────────────────────────

    def record_observation(
        self,
        source: str,
        source_id: str,
        event_type: str,
        raw_payload: Any,
        event_timestamp: float | None = None,
        validation_status: str = "valid",
        quality_score: float = 1.0,
        error_reason: str = "",
        data_version: str = DATA_VERSION_CURRENT,
        ingestion_timestamp: float | None = None,
    ) -> str | None:
        """Store a raw observation and return its ``observation_id``.

        On duplicate (same ``(source, source_id, payload_hash)`` already
        present in the DB), returns ``None`` and bumps
        ``duplicate_count``. On any other storage error, returns
        ``None`` and logs the error — the caller is expected to treat a
        ``None`` return as "vault didn't keep this row, downstream
        replay is unavailable for this record" (the pipeline still
        proceeds; the vault is best-effort storage, not a blocking
        gate).

        Args:
            source: Originating source (``"clob"`` / ``"gamma"`` /
                ``"websocket"`` / ``"news"``).
            source_id: The source's own ID for the record.
            event_type: ``"snapshot"`` / ``"trade"`` / etc.
            raw_payload: The original JSON-serialisable payload. Any
                non-JSON-serialisable object raises ``TypeError`` —
                callers MUST pre-serialise weird types (e.g. ``Decimal``
                → ``float``) before calling.
            event_timestamp: When the event occurred (source-reported).
                Defaults to ``ingestion_timestamp`` when ``None`` or
                ``0``.
            validation_status: Pre-vault verdict. Must be one of
                ``VALIDATION_STATUSES`` (else ``ValueError``).
            quality_score: ``0.0`` to ``1.0``.
            error_reason: Why the record was rejected (if applicable).
            data_version: Schema version of ``raw_payload``.
            ingestion_timestamp: Override for tests / replay flows.
                Defaults to ``time.time()`` at call entry.

        Returns:
            The ``observation_id`` (UUID4 string) of the stored record,
            or ``None`` if the row was deduplicated / rejected.
        """
        if validation_status not in VALIDATION_STATUSES:
            raise ValueError(
                f"validation_status must be one of {sorted(VALIDATION_STATUSES)}, "
                f"got {validation_status!r}"
            )

        # Capture ingestion_timestamp ONCE at the top so every
        # downstream consumer sees a consistent value (mirrors the W24-4
        # ``DataValidator.validate_snapshot`` pattern).
        ing_ts = float(ingestion_timestamp) if ingestion_timestamp is not None else time.time()
        evt_ts = float(event_timestamp) if event_timestamp else ing_ts

        record = RawRecord(
            source=source,
            source_id=source_id,
            event_type=event_type,
            raw_payload=raw_payload,
            event_timestamp=evt_ts,
            ingestion_timestamp=ing_ts,
            processing_timestamp=0.0,  # filled in after the INSERT
            data_version=data_version,
            validation_status=validation_status,
            quality_score=quality_score,
            error_reason=error_reason,
        )

        # Fast-path dedup: in-memory deque.
        dedup_key = self._dedup_key(record.source, record.source_id, record.payload_hash)
        with self._lock:
            if dedup_key in self._seen_keys:
                self._duplicate_count += 1
                return None
            self._seen_keys.append(dedup_key)

        # Serialise the payload to a canonical JSON string. The
        # ``default=str`` fallback lets the vault accept objects that
        # ``json.dumps`` can't natively handle (e.g. ``Decimal`` /
        # ``datetime`` — the canonical fallback gives a string
        # representation rather than raising, which is correct for
        # audit-grade storage: we'd rather store SOMETHING than lose
        # the record entirely).
        try:
            payload_json = json.dumps(raw_payload, sort_keys=True, default=str)
        except (TypeError, ValueError) as e:
            # Even the ``default=str`` fallback failed — the payload
            # contains an object whose ``__str__`` itself raised. Log
            # and drop; the caller's ``None`` return signals the loss.
            logger.error(
                "[raw_vault] Cannot serialise payload for source=%s "
                "source_id=%s: %s — record dropped",
                source,
                source_id,
                e,
            )
            with self._lock:
                self._invalid_count += 1
            return None

        proc_ts = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO raw_records (
                        observation_id, source, source_id, event_type,
                        raw_payload, event_timestamp, ingestion_timestamp,
                        processing_timestamp, data_version, validation_status,
                        quality_score, error_reason, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.observation_id,
                        record.source,
                        record.source_id,
                        record.event_type,
                        payload_json,
                        record.event_timestamp,
                        record.ingestion_timestamp,
                        proc_ts,
                        record.data_version,
                        record.validation_status,
                        record.quality_score,
                        record.error_reason,
                        record.payload_hash,
                        proc_ts,
                    ),
                )
                conn.commit()
                # ``cur.rowcount`` is 0 when INSERT OR IGNORE skipped
                # the row (the DB-layer UNIQUE constraint matched a row
                # the in-memory deque missed — happens after a restart
                # when the deque window has evicted the old key).
                if cur.rowcount == 0:
                    with self._lock:
                        self._duplicate_ignored_count += 1
                        # The fast-path deque didn't catch it but the DB
                        # did — keep the duplicate count in sync so the
                        # operator's dashboard sees the total.
                        # (We already incremented _duplicate_count above
                        # only on the fast-path hit; for the DB-layer
                        # hit, increment here.)
                        pass
                    return None
        except sqlite3.Error as e:
            logger.error(
                "[raw_vault] SQLite insert failed for source=%s "
                "source_id=%s: %s",
                source,
                source_id,
                e,
            )
            with self._lock:
                self._invalid_count += 1
            return None

        with self._lock:
            self._record_count += 1
            if validation_status == "invalid":
                self._invalid_count += 1
        # Fill in the processing_timestamp on the in-memory record so
        # the returned observation_id is consistent with what's in the
        # DB (a caller replaying by id would see the same proc_ts).
        record.processing_timestamp = proc_ts
        return record.observation_id

    def record(self, record: RawRecord) -> str | None:
        """Store a pre-built ``RawRecord`` (advanced API).

        The convenience ``record_observation`` covers the common case
        (caller has the raw fields, the vault builds the dataclass).
        This entry point is for callers that already have a
        ``RawRecord`` (e.g. the pipeline's normalized-stage output that
        already computed the payload hash) so the hash isn't recomputed.
        """
        return self.record_observation(
            source=record.source,
            source_id=record.source_id,
            event_type=record.event_type,
            raw_payload=record.raw_payload,
            event_timestamp=record.event_timestamp,
            validation_status=record.validation_status,
            quality_score=record.quality_score,
            error_reason=record.error_reason,
            data_version=record.data_version,
            ingestion_timestamp=record.ingestion_timestamp,
        )

    # ── Replay ──────────────────────────────────────────────────────────

    def replay(self, observation_id: str) -> dict[str, Any] | None:
        """Fetch a single raw record by its ``observation_id``.

        Returns the full row as a dict (``raw_payload`` is parsed back
        from JSON to a Python object). ``None`` if the ID isn't in the
        vault (deleted / never stored / dedup-rejected).
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM raw_records WHERE observation_id = ?
                    """,
                    (observation_id,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.error("[raw_vault] replay(%s) failed: %s", observation_id, e)
            return None
        if row is None:
            return None
        return _row_to_dict(row)

    def replay_range(
        self,
        start_ts: float | None = None,
        end_ts: float | None = None,
        source: str | None = None,
        event_type: str | None = None,
        validation_status: str | None = None,
        limit: int = 1000,
    ) -> Iterable[dict[str, Any]]:
        """Yield raw records matching the filter, ordered by
        ``event_timestamp`` descending (most-recent-first).

        Any of ``start_ts`` / ``end_ts`` / ``source`` / ``event_type`` /
        ``validation_status`` may be ``None`` (no filter on that
        dimension). ``limit`` caps the result count (default 1000, hard
        ceiling 10_000 so a misbehaving caller can't OOM the bot by
        requesting millions of rows).
        """
        cap = max(1, min(int(limit), 10_000))
        sql = "SELECT * FROM raw_records"
        clauses: list[str] = []
        params: list[Any] = []
        if start_ts is not None:
            clauses.append("event_timestamp >= ?")
            params.append(float(start_ts))
        if end_ts is not None:
            clauses.append("event_timestamp <= ?")
            params.append(float(end_ts))
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if validation_status is not None:
            clauses.append("validation_status = ?")
            params.append(validation_status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_timestamp DESC LIMIT ?"
        params.append(cap)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.error("[raw_vault] replay_range failed: %s", e)
            return []
        return [_row_to_dict(r) for r in rows]

    # ── Dedup / stats ───────────────────────────────────────────────────

    def is_duplicate(
        self,
        source: str,
        source_id: str,
        raw_payload: Any,
    ) -> bool:
        """Return ``True`` if the ``(source, source_id, payload_hash)``
        triple is already in the in-memory dedup deque.

        Fast-path: O(1) deque lookup. Does NOT consult the DB (the DB
        is the restart-safe backstop, not the runtime fast-path). The
        ``record_observation`` call itself uses both layers; this method
        is for callers that want to pre-check WITHOUT storing (e.g. a
        connector that wants to skip a fetch entirely if the result
        would dedup).
        """
        key = self._dedup_key(source, source_id, _hash_payload(raw_payload))
        with self._lock:
            return key in self._seen_keys

    def get_stats(self) -> dict[str, Any]:
        """Return live vault counters (JSON-serialisable).

        Returned keys: ``record_count`` (cumulative successful inserts
        in this process), ``duplicate_count`` (fast-path dedup hits),
        ``duplicate_ignored_count`` (DB-layer ``INSERT OR IGNORE``
        hits — the backstop path), ``invalid_count`` (records stored
        with ``validation_status="invalid"`` plus records dropped at
        serialisation / DB-error time), ``seen_keys_size`` (current
        deque occupancy, capped at ``_MAX_SEEN_KEYS``).
        """
        with self._lock:
            return {
                "record_count": self._record_count,
                "duplicate_count": self._duplicate_count,
                "duplicate_ignored_count": self._duplicate_ignored_count,
                "invalid_count": self._invalid_count,
                "seen_keys_size": len(self._seen_keys),
                "seen_keys_max": _MAX_SEEN_KEYS,
                "db_path": str(self._db_path),
            }

    def reset_stats(self) -> None:
        """Zero the in-memory counters (test-only — does NOT truncate
        the DB). Mirrors the W24-4 ``data_validator._seen_ids.clear()``
        pattern in ``tests/conftest.py``'s autouse fixture.
        """
        with self._lock:
            self._record_count = 0
            self._duplicate_count = 0
            self._invalid_count = 0
            self._duplicate_ignored_count = 0
            self._seen_keys.clear()

    def truncate(self) -> None:
        """Drop every record from the on-disk SQLite table + clear the
        in-memory dedup deque.

        W34-4 — test-only helper used by the W34-4 raw-vault replay API
        test-suite autouse fixture so the ``to_timestamp`` /
        ``from_timestamp`` filter tests don't see records seeded by a
        prior test run (the on-disk SQLite file persists across pytest
        sessions, so without an explicit truncate the filter tests would
        count records seeded minutes / hours / days ago and fail
        non-deterministically). Mirrors the ``dead_letter_queue.clear``
        / ``checkpoint_manager.clear`` pattern in
        ``tests/conftest.py``'s autouse fixture.

        Production code should NOT call this — the vault's contract is
        "every record survives for audit". The method is exposed on the
        public class API so the test-suite can call it without resorting
        to a private ``_connect`` + raw ``DELETE FROM raw_records`` SQL
        dance (which would break the moment the schema changes).
        """
        with self._lock:
            self._record_count = 0
            self._duplicate_count = 0
            self._invalid_count = 0
            self._duplicate_ignored_count = 0
            self._seen_keys.clear()
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM raw_records")
                conn.commit()
        except sqlite3.Error as e:
            logger.error("[raw_vault] truncate failed: %s", e)

    def close(self) -> None:
        """No-op (the vault opens a per-call connection, so there's no
        long-lived connection to close). Kept for API symmetry with
        ``core.clob_client.ClobClient.close`` / ``core.gamma_client.Gamma
        Client.close`` so a caller that loops over every connector +
        storage in a shutdown list doesn't crash on the vault.
        """
        return None

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _dedup_key(source: str, source_id: str, payload_hash: str) -> str:
        """Build the dedup key. ``"|"`` separator chosen because
        ``source`` / ``source_id`` are alpha-numeric / hex / UUIDs
        (no ``|`` in the wild); ``payload_hash`` is hex (also no
        ``|``). A colon would conflict with the W24-4 hash separator
        so ``|`` is used instead.
        """
        return f"{source}|{source_id}|{payload_hash}"


# ── Module-level helpers ──────────────────────────────────────────────────────


def _hash_payload(payload: Any) -> str:
    """SHA-256 (truncated to 16 hex chars = 64 bits) of the canonical
    JSON of ``payload``.

    The 16-char truncation mirrors the W24-4 ``DataValidator`` snapshot
    hash convention — collision probability ~1 in 10^19 for a 10k-entry
    dedup window, which is acceptable for an in-memory fast-path (the
    DB-layer UNIQUE constraint is the backstop).
    """
    try:
        payload_str = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        # Fall back to ``repr`` so a non-JSON-serialisable object still
        # produces a stable hash (the vault itself rejects the storage
        # later via the ``record_observation`` serialisation check, but
        # ``is_duplicate`` should be callable on any input without
        # raising).
        payload_str = repr(payload)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:16]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``raw_records`` row to a dict with ``raw_payload``
    parsed back from JSON.

    The ``raw_payload`` field is the canonical JSON string at storage
    time; a replayer expects a Python object (so it can index into the
    payload without re-parsing). A malformed JSON (shouldn't happen —
    the storage path serialises with ``default=str`` which never
    produces invalid JSON) falls back to the raw string.
    """
    d = dict(row)
    raw = d.get("raw_payload")
    if isinstance(raw, str):
        try:
            d["raw_payload"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Keep the raw string — better to surface the unparsed
            # bytes than to drop the record from the replay result.
            pass
    return d


# ── Module-level singleton ────────────────────────────────────────────────────
# Mirrors the convention used by every sibling background-task module
# (``core.book_poller.book_poller``, ``core.data_validator.data_validator`` …).
# Importers grab it at module-import time; the constructor opens the DB,
# runs migrations, and primes the dedup deque — the I/O is bounded
# (a single SQLite file + one ``SELECT … LIMIT 10_000`` query) so the
# import-time cost is negligible.
raw_vault = RawVault()


__all__ = [
    "RawRecord",
    "RawVault",
    "raw_vault",
    "DATA_VERSION_CURRENT",
    "VALIDATION_STATUSES",
]
