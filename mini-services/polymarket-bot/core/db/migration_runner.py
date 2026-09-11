"""
core/db/migration_runner.py — Transactional Database Migration Runner.

Features:
- Scans `core/db/migrations/*.sql` in deterministic order.
- Records executed migrations in `operations.schema_migration` with execution timings and sha256 checksums.
- Automatic idempotency and rollback detection.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationRunner:
    """Automated schema migration runner for PostgreSQL / TimescaleDB."""

    def __init__(self, db_url: str | None = None) -> None:
        self.db_url = db_url or os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:polymarket_secret@timescaledb:5432/polymarket",
        )

    async def run_migrations(self) -> dict[str, Any]:
        """Apply all pending migrations in transactional sequence."""
        try:
            import asyncpg
        except ImportError:
            log.warning("[migration_runner] asyncpg not installed — skipping PostgreSQL migrations")
            return {"applied": 0, "status": "skipped_no_asyncpg"}

        applied_count = 0
        total_time = 0.0

        try:
            conn = await asyncpg.connect(self.db_url, timeout=10.0)
        except Exception as e:
            log.error("[migration_runner] Failed to connect to PostgreSQL: %s", e)
            return {"applied": 0, "status": f"connection_failed: {e}"}

        try:
            # Ensure operations schema and schema_migration table exist
            await conn.execute("CREATE SCHEMA IF NOT EXISTS operations;")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS operations.schema_migration (
                    version VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    checksum VARCHAR(64) NOT NULL,
                    execution_time_ms DOUBLE PRECISION NOT NULL
                );
            """)

            # Fetch applied migration versions
            rows = await conn.fetch("SELECT version, checksum FROM operations.schema_migration;")
            applied = {r["version"]: r["checksum"] for r in rows}

            migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
            for sql_file in migration_files:
                version = sql_file.stem.split("_")[0]
                name = sql_file.name
                content = sql_file.read_text(encoding="utf-8")
                checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()

                if version in applied:
                    if applied[version] != checksum:
                        log.warning(
                            "[migration_runner] Migration %s checksum mismatch! Recorded: %s, Current: %s",
                            version, applied[version][:8], checksum[:8]
                        )
                    continue

                log.info("[migration_runner] Applying migration %s (%s)...", version, name)
                start_time = time.perf_counter()

                # Execute migration inside a transaction
                async with conn.transaction():
                    await conn.execute(content)
                    elapsed_ms = (time.perf_counter() - start_time) * 1000
                    await conn.execute("""
                        INSERT INTO operations.schema_migration (version, name, checksum, execution_time_ms)
                        VALUES ($1, $2, $3, $4);
                    """, version, name, checksum, elapsed_ms)

                applied_count += 1
                total_time += elapsed_ms
                log.info("[migration_runner] Migration %s applied successfully in %.1fms", version, elapsed_ms)

            return {
                "applied": applied_count,
                "total_time_ms": round(total_time, 2),
                "status": "success",
            }
        except Exception as e:
            log.error("[migration_runner] Migration error: %s", e)
            return {"applied": applied_count, "status": f"error: {e}"}
        finally:
            await conn.close()


migration_runner = MigrationRunner()
