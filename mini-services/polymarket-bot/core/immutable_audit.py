"""Cryptographically immutable audit trail using hash chaining.

Each audit entry includes the SHA-256 hash of the previous entry,
creating a tamper-evident chain. Any modification to a past entry
breaks the chain, which is detectable via verification.

Usage:
    from core.immutable_audit import immutable_audit
    immutable_audit.log("trade_executed", {"token_id": "...", "size": 10})
    is_valid = immutable_audit.verify_chain()

W17-5 — additive wiring. The existing ``core/audit_logger.py`` durable
SQLite trail is left untouched (it remains the asynchronous, category-
indexed event log used throughout the trading pipeline). This module
introduces a SECOND, hash-chained trail for high-sensitivity control
events (trade execution, position close, kill switch activation /
deactivation, live trading enable, config changes, feature flag
changes). Any tampering with a past row — modifying the event_type,
payload, or previous_hash — breaks the SHA-256 chain, surfaced as
``verify_chain() -> {"valid": False, "broken_at": <entry_id>}``.

The chain lives in its own SQLite db (``IMMUTABLE_AUDIT_DB``, default
``/app/data/immutable_audit.db``) so the existing audit_trail.db
schema / indexes / async writers are not perturbed — mirrors the
additive-isolation pattern established by ``core/decision_ledger``
and ``core/closed_positions``.

HTTP surface (registered by ``register_routes`` and wired in
``api/server.py``):

    GET  /api/audit/immutable          recent entries (paginated)
    GET  /api/audit/immutable/verify   verify the integrity of the chain
    GET  /api/audit/immutable/stats    aggregate entry stats
    POST /api/audit/immutable/log      manually log an event (testing)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

IMMUTABLE_AUDIT_DB = Path(
    os.environ.get("IMMUTABLE_AUDIT_DB", "/app/data/immutable_audit.db")
)

# Genesis hash: SHA-256 hex of 64 zeros. Every chain starts with
# ``previous_hash == GENESIS_HASH`` so the first entry's link is verifiable.
GENESIS_HASH = "0" * 64


@dataclass
class AuditEntry:
    entry_id: int
    timestamp: float
    event_type: str
    payload: str  # JSON
    previous_hash: str
    entry_hash: str
    sequence: int  # Monotonic sequence number


class ImmutableAuditTrail:
    """Hash-chained audit trail for tamper evidence."""

    def __init__(self, db_path: Path = IMMUTABLE_AUDIT_DB) -> None:
        self._db_path = db_path
        self._last_hash = GENESIS_HASH
        self._sequence = 0
        self._init_db()
        self._load_last_entry()

    def _init_db(self) -> None:
        """Create the ``audit_chain`` table if absent.

        Defensive: a read-only filesystem (sandbox) means the mkdir /
        connect silently fails — the singleton stays constructed with
        ``_last_hash`` at the genesis value, and every ``log`` call
        re-attempts the insert (and logs the failure). Mirrors the
        swallow-init-failure pattern used by ``core.feature_flags`` /
        ``core.decision_ledger``.
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # pragma: no cover — sandbox-only failure mode
            logger.warning(
                "[immutable_audit] cannot create db dir %s: %s",
                self._db_path.parent,
                e,
            )
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS audit_chain (
                        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        previous_hash TEXT NOT NULL,
                        entry_hash TEXT NOT NULL,
                        sequence INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_chain(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_chain(event_type);
                    CREATE INDEX IF NOT EXISTS idx_audit_hash ON audit_chain(entry_hash);
                    """
                )
        except sqlite3.Error as e:  # pragma: no cover — sandbox-only failure mode
            logger.warning("[immutable_audit] db init failed at %s: %s", self._db_path, e)

    def _load_last_entry(self) -> None:
        """Load the last entry to continue the chain.

        On a fresh DB (no rows) ``_last_hash`` stays at ``GENESIS_HASH``
        and ``_sequence`` stays at 0 — the first ``log`` call will
        produce ``entry_id=1``, ``sequence=1``, ``previous_hash=GENESIS_HASH``.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT entry_hash, sequence FROM audit_chain "
                    "ORDER BY entry_id DESC LIMIT 1"
                ).fetchone()
                if row:
                    self._last_hash = row[0]
                    self._sequence = row[1]
        except sqlite3.Error as e:
            logger.warning("[immutable_audit] _load_last_entry failed: %s", e)

    def _compute_hash(
        self,
        timestamp: float,
        event_type: str,
        payload: str,
        previous_hash: str,
        sequence: int,
    ) -> str:
        """Compute SHA-256 hash of the entry.

        The hash covers every field that defines the entry's identity
        (timestamp, event_type, payload, previous_hash, sequence).
        Modifying ANY of these without re-computing ``entry_hash`` will
        be detected by ``verify_chain``.
        """
        data = f"{timestamp}:{event_type}:{payload}:{previous_hash}:{sequence}"
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def log(self, event_type: str, payload: dict[str, Any]) -> Optional[AuditEntry]:
        """Add a new entry to the audit chain.

        Returns the :class:`AuditEntry` on success, or ``None`` on
        persistence failure (logged at WARNING — the caller's request
        path is not blocked because the chain is best-effort: a broken
        write should never crash the trading pipeline).

        Payload is JSON-serialised with ``sort_keys=True`` so the same
        dict always produces the same hash (a dict with reordered keys
        must not break the chain retroactively). Non-JSON-serialisable
        values are stringified via ``default=str`` rather than raising.
        """
        try:
            timestamp = time.time()
            self._sequence += 1
            payload_str = json.dumps(payload, sort_keys=True, default=str)
            previous_hash = self._last_hash
            entry_hash = self._compute_hash(
                timestamp, event_type, payload_str, previous_hash, self._sequence
            )

            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO audit_chain
                        (timestamp, event_type, payload, previous_hash, entry_hash, sequence)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        event_type,
                        payload_str,
                        previous_hash,
                        entry_hash,
                        self._sequence,
                    ),
                )
                entry_id = cursor.lastrowid

            self._last_hash = entry_hash

            entry = AuditEntry(
                entry_id=entry_id,  # type: ignore[arg-type]
                timestamp=timestamp,
                event_type=event_type,
                payload=payload_str,
                previous_hash=previous_hash,
                entry_hash=entry_hash,
                sequence=self._sequence,
            )

            logger.debug(
                "Audit entry #%d: %s (hash=%s...)",
                self._sequence,
                event_type,
                entry_hash[:16],
            )
            return entry
        except Exception as e:  # noqa: BLE001 — best-effort: never block the caller
            logger.warning(
                "[immutable_audit] log failed for event_type=%s: %s",
                event_type,
                e,
            )
            # Roll back the in-memory sequence bump so the next call
            # doesn't skip a number — the on-disk chain didn't advance.
            if self._sequence > 0:
                self._sequence -= 1
            return None

    def verify_chain(self, start_id: int = 1, end_id: int | None = None) -> dict[str, Any]:
        """Verify the integrity of the audit chain.

        Returns:
            Dict with ``valid`` (bool), ``broken_at`` (entry_id or None),
            ``checked`` (count), and ``last_hash`` (the verified tail
            hash, or None if the chain was broken / empty).
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                if end_id:
                    rows = conn.execute(
                        "SELECT * FROM audit_chain WHERE entry_id >= ? AND entry_id <= ? "
                        "ORDER BY entry_id",
                        (start_id, end_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM audit_chain WHERE entry_id >= ? ORDER BY entry_id",
                        (start_id,),
                    ).fetchall()
        except sqlite3.Error as e:
            logger.warning("[immutable_audit] verify_chain read failed: %s", e)
            return {
                "valid": False,
                "broken_at": None,
                "checked": 0,
                "last_hash": None,
                "error": str(e),
            }

        if not rows:
            return {
                "valid": True,
                "broken_at": None,
                "checked": 0,
                "last_hash": None,
                "message": "No entries to verify",
            }

        # For start_id == 1, the genesis hash is the expected
        # ``previous_hash`` of the first row. For start_id > 1, we
        # trust the first row's stored ``previous_hash`` (i.e. the
        # link from the boundary row's predecessor is NOT re-verified
        # here — that's a property of the larger range, not the
        # sub-range being verified).
        previous_hash = GENESIS_HASH if start_id == 1 else rows[0]["previous_hash"]
        broken_at: int | None = None

        for row in rows:
            # Verify the chain link: the stored previous_hash must
            # equal the previous row's entry_hash (or the genesis hash
            # for the first row when starting from id=1).
            if row["previous_hash"] != previous_hash:
                broken_at = row["entry_id"]
                break

            # Verify the entry's own hash: recompute it from the
            # stored fields and compare against the stored entry_hash.
            computed_hash = self._compute_hash(
                row["timestamp"],
                row["event_type"],
                row["payload"],
                row["previous_hash"],
                row["sequence"],
            )
            if computed_hash != row["entry_hash"]:
                broken_at = row["entry_id"]
                break

            previous_hash = row["entry_hash"]

        return {
            "valid": broken_at is None,
            "broken_at": broken_at,
            "checked": len(rows),
            "last_hash": previous_hash if broken_at is None else None,
        }

    def get_entries(
        self,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retrieve audit entries (most recent first).

        ``event_type`` optionally filters to a single event type.
        ``limit`` is clamped to ``[1, 1000]`` for safety; ``offset``
        supports pagination for the dashboard's audit-trail view.
        """
        limit = max(1, min(1000, int(limit)))
        offset = max(0, int(offset))
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                if event_type:
                    rows = conn.execute(
                        "SELECT * FROM audit_chain WHERE event_type = ? "
                        "ORDER BY entry_id DESC LIMIT ? OFFSET ?",
                        (event_type, limit, offset),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM audit_chain ORDER BY entry_id DESC "
                        "LIMIT ? OFFSET ?",
                        (limit, offset),
                    ).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.warning("[immutable_audit] get_entries failed: %s", e)
            return []

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate stats for the chain.

        Used by ``GET /api/audit/immutable/stats`` so the dashboard can
        surface total entries, per-event-type counts, latest timestamp,
        and the current tail hash + sequence number.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM audit_chain"
                ).fetchone()[0]
                by_type = conn.execute(
                    "SELECT event_type, COUNT(*) as count FROM audit_chain "
                    "GROUP BY event_type ORDER BY count DESC"
                ).fetchall()
                latest = conn.execute(
                    "SELECT timestamp FROM audit_chain "
                    "ORDER BY entry_id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning("[immutable_audit] get_stats failed: %s", e)
            return {
                "total_entries": 0,
                "event_types": {},
                "latest_timestamp": None,
                "last_hash": self._last_hash,
                "sequence": self._sequence,
            }

        return {
            "total_entries": total,
            "event_types": {row[0]: row[1] for row in by_type},
            "latest_timestamp": latest[0] if latest else None,
            "last_hash": self._last_hash,
            "sequence": self._sequence,
        }


# Module-level singleton — production callers do
# ``from core.immutable_audit import immutable_audit`` then
# ``immutable_audit.log("trade_executed", {...})``. Constructed at
# import time so the chain is ready before the first request lands.
immutable_audit = ImmutableAuditTrail()


# ── FastAPI route registration ─────────────────────────────────────────────
# The ``AuditLogRequest`` Pydantic model is declared at module scope (NOT
# inside ``register_routes``) because this file uses ``from __future__
# import annotations`` (PEP 563) — every annotation is a string at
# runtime, and FastAPI resolves the string by looking up the handler's
# ``__globals__`` (the module namespace). A locally-scoped model would
# resolve to ``None`` and FastAPI would fall back to treating ``body``
# as a query parameter (returning 422 "Field required" on a JSON POST).
try:  # Pydantic v2 — optional at module load if FastAPI is not installed.
    from pydantic import BaseModel, Field

    class AuditLogRequest(BaseModel):
        """Request body for ``POST /api/audit/immutable/log``.

        ``event_type`` is a free-form string (no enum constraint) so
        the chain can record arbitrary control events. ``payload`` is
        an arbitrary JSON object — the chain serialises it with
        ``sort_keys=True`` so the same dict always produces the same
        hash (reordered keys do not break the chain retroactively).
        """

        event_type: str = Field(..., min_length=1, max_length=200)
        payload: dict[str, Any] = Field(default_factory=dict)
except ImportError:  # pragma: no cover — defensive: pydantic is required
    # by FastAPI; if it's missing the routes can't be registered anyway.
    AuditLogRequest = None  # type: ignore[assignment, misc]


def register_routes(app: Any) -> None:
    """Append immutable-audit inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET  /api/audit/immutable
          Recent entries (most recent first). Query params:

          - ``limit``   (1..1000, default 100) — max rows to return
          - ``offset``  (default 0)           — pagination offset
          - ``event_type`` (optional)         — filter to a single type

          Returns ``{count, entries[]}``. Each entry carries every
          column from ``audit_chain`` (``entry_id``, ``timestamp``,
          ``event_type``, ``payload`` (JSON string), ``previous_hash``,
          ``entry_hash``, ``sequence``).

      GET  /api/audit/immutable/verify
          Verify the integrity of the hash chain. Query params:

          - ``start_id`` (default 1) — first entry_id to verify
          - ``end_id``   (optional) — last entry_id to verify

          Returns ``{valid, broken_at, checked, last_hash}``. If the
          chain is intact, ``valid`` is ``True`` and ``broken_at`` is
          ``None``; otherwise ``broken_at`` is the ``entry_id`` of the
          first tampered row.

      GET  /api/audit/immutable/stats
          Aggregate stats for the chain — ``total_entries``,
          ``event_types`` (per-type counts), ``latest_timestamp``,
          ``last_hash``, ``sequence``.

      POST /api/audit/immutable/log
          Manually append an entry to the chain (testing / debugging).
          Body: ``{event_type: str, payload: dict}``. Returns the
          inserted entry (with its hash) on success or 500 on failure.
    """
    from fastapi import HTTPException, Query  # local import — FastAPI optional at module load

    @app.get("/api/audit/immutable", tags=["audit"])
    async def _list_immutable_entries(
        limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
        offset: int = Query(0, ge=0, description="Pagination offset"),
        event_type: str | None = Query(
            None, description="Filter by event_type (e.g. trade_executed)"
        ),
    ):
        """Return recent hash-chained audit entries (most recent first)."""
        entries = immutable_audit.get_entries(
            event_type=event_type, limit=limit, offset=offset
        )
        return {"count": len(entries), "entries": entries}

    @app.get("/api/audit/immutable/verify", tags=["audit"])
    async def _verify_immutable_chain(
        start_id: int = Query(1, ge=1, description="First entry_id to verify"),
        end_id: int | None = Query(
            None, ge=1, description="Last entry_id to verify (default: latest)"
        ),
    ):
        """Verify the integrity of the hash chain.

        Returns ``{valid, broken_at, checked, last_hash}``. A broken
        chain (tampering detected) returns HTTP 200 with ``valid=false``
        — the operator dashboard surfaces this as a critical alert
        rather than a 5xx (the endpoint itself is working correctly; the
        chain is what's broken).
        """
        return immutable_audit.verify_chain(start_id=start_id, end_id=end_id)

    @app.get("/api/audit/immutable/stats", tags=["audit"])
    async def _immutable_audit_stats():
        """Return aggregate stats for the immutable audit chain."""
        return immutable_audit.get_stats()

    @app.post("/api/audit/immutable/log", tags=["audit"])
    async def _log_immutable_entry(req: AuditLogRequest):
        """Manually append an entry to the immutable audit chain.

        Useful for testing / debugging — production callers should use
        the ``immutable_audit`` singleton directly (e.g. inside the
        ``/api/trade`` handler) so the entry is logged in-process
        without an HTTP round-trip.
        """
        entry = immutable_audit.log(req.event_type, req.payload)
        if entry is None:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Failed to append to immutable audit chain — see "
                    "server logs (the singleton's log() returned None)."
                ),
            )
        return {
            "ok": True,
            "entry": {
                "entry_id": entry.entry_id,
                "timestamp": entry.timestamp,
                "event_type": entry.event_type,
                "payload": entry.payload,
                "previous_hash": entry.previous_hash,
                "entry_hash": entry.entry_hash,
                "sequence": entry.sequence,
            },
        }


__all__ = [
    "AuditEntry",
    "AuditLogRequest",
    "GENESIS_HASH",
    "IMMUTABLE_AUDIT_DB",
    "ImmutableAuditTrail",
    "immutable_audit",
    "register_routes",
]
