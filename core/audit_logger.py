"""
core/audit_logger.py — Durable SQLite Audit Trail & Execution Journal.

Provides immutable persistence for all trading signals, orders, fills, risk events,
model predictions, and fundamental ingestions with millisecond timestamps and idempotency keys.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("AUDIT_DB_PATH", "/app/data/audit_trail.db"))


class AuditLogger:
    """
    Asynchronous, SQLite-backed immutable audit trail logger.
    """

    def __init__(self) -> None:
        self._db_path = DB_PATH
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    token_id TEXT,
                    slug TEXT,
                    details TEXT NOT NULL,
                    pnl REAL DEFAULT 0.0,
                    strategy TEXT,
                    idempotency_key TEXT UNIQUE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_cat ON audit_events(category)")
            conn.commit()

    async def log_event(
        self,
        category: str,
        event_type: str,
        details: str,
        token_id: str | None = None,
        slug: str | None = None,
        pnl: float = 0.0,
        strategy: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Record an immutable audit event asynchronously."""
        ts = time.time()
        if not idempotency_key:
            idempotency_key = f"{category}_{event_type}_{ts}_{os.urandom(4).hex()}"

        def _insert():
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO audit_events 
                        (timestamp, category, event_type, token_id, slug, details, pnl, strategy, idempotency_key)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ts, category, event_type, token_id, slug, details, pnl, strategy, idempotency_key),
                    )
                    conn.commit()
            except Exception as e:
                log.error("[audit_logger] Failed to write event: %s", e)

        await asyncio.to_thread(_insert)

    async def get_recent_events(self, limit: int = 100, category: str | None = None) -> list[dict[str, Any]]:
        """Fetch recent immutable audit logs."""
        def _fetch():
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                if category and category != "all":
                    cursor.execute(
                        "SELECT * FROM audit_events WHERE category = ? ORDER BY timestamp DESC LIMIT ?",
                        (category, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?",
                        (limit,),
                    )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_fetch)


# Global singleton
audit_logger = AuditLogger()
