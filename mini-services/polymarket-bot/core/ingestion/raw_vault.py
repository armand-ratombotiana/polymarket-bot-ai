"""
core/ingestion/raw_vault.py — Raw Ingestion Vault & Provenance Engine.

Features:
- Immutable raw observation recording with SHA-256 payload checksums.
- Bitemporal timestamps (occurred_at, received_at, ingested_at).
- Dead-letter record quarantine with full error context and stack traces.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any

from core.timescale_db import timescale_db

log = logging.getLogger(__name__)


class RawVault:
    """Raw Observation Ingestion Vault for PostgreSQL / TimescaleDB."""

    async def record_observation(
        self,
        source_id: str,
        raw_payload: dict[str, Any] | list[Any] | str,
        occurred_at: datetime.datetime | None = None,
    ) -> str | None:
        """Store immutable raw payload with SHA-256 hash and return observation_id."""
        try:
            if isinstance(raw_payload, (dict, list)):
                payload_str = json.dumps(raw_payload, sort_keys=True)
                payload_json = raw_payload
            else:
                payload_str = str(raw_payload)
                try:
                    payload_json = json.loads(payload_str)
                except Exception:
                    payload_json = {"raw": payload_str}

            checksum = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
            now = datetime.datetime.now(datetime.timezone.utc)
            occ_dt = occurred_at or now

            if timescale_db._is_postgres and timescale_db._pool:
                async with timescale_db._pool.acquire() as conn:
                    obs_id = await conn.fetchval("""
                        INSERT INTO raw.raw_observation (
                            source_id, payload_checksum, raw_payload, occurred_at, received_at, parse_status
                        )
                        VALUES ($1, $2, $3, $4, $5, 'PARSED')
                        RETURNING observation_id::text;
                    """, source_id, checksum, json.dumps(payload_json), occ_dt, now)
                    return str(obs_id)
        except Exception as e:
            log.error("[raw_vault] Failed to store raw observation for source %s: %s", source_id, e)
            await self.quarantine_record(source_id, str(raw_payload), type(e).__name__, str(e))
        return None

    async def quarantine_record(
        self,
        source_id: str,
        raw_payload: str,
        error_class: str,
        error_message: str,
        stack_trace: str = "",
    ) -> None:
        """Record corrupted or unparseable payload to raw.dead_letter_record."""
        try:
            if timescale_db._is_postgres and timescale_db._pool:
                async with timescale_db._pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO raw.dead_letter_record (
                            source_id, raw_payload, error_class, error_message, stack_trace
                        )
                        VALUES ($1, $2, $3, $4, $5);
                    """, source_id, raw_payload, error_class, error_message, stack_trace)
        except Exception as e:
            log.error("[raw_vault] Failed to record dead-letter entry: %s", e)


raw_vault = RawVault()
