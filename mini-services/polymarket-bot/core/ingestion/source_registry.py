"""
core/ingestion/source_registry.py — Universal Source Registry.

Provides dynamic, verified tracking of external APIs, feeds, and connectors.
"""
from __future__ import annotations

import logging
from typing import Any

from core.timescale_db import timescale_db

log = logging.getLogger(__name__)


class SourceRegistry:
    """Truthful, dynamic source registry backed by PostgreSQL / TimescaleDB."""

    async def get_all_sources(self) -> list[dict[str, Any]]:
        """Retrieve all active registered sources."""
        if timescale_db._is_postgres and timescale_db._pool:
            try:
                async with timescale_db._pool.acquire() as conn:
                    rows = await conn.fetch("""
                        SELECT source_id, name, domain, source_type, endpoint_url,
                               rate_limit_rps, credibility_score, is_active,
                               records_observed, records_accepted, records_errored,
                               last_success_at, last_error_at
                        FROM raw.source_registry
                        ORDER BY is_active DESC, name ASC;
                    """)
                    return [dict(r) for r in rows]
            except Exception as e:
                log.error("[source_registry] Failed to fetch sources: %s", e)

        # Fallback local in-memory/standby defaults
        return [
            {
                "source_id": "clob_rest",
                "name": "Polymarket CLOB REST API",
                "domain": "clob.polymarket.com",
                "source_type": "clob_rest",
                "endpoint_url": "https://clob.polymarket.com",
                "rate_limit_rps": 10.0,
                "credibility_score": 1.0,
                "is_active": True,
                "records_observed": 0,
                "records_accepted": 0,
                "records_errored": 0,
            },
            {
                "source_id": "gamma_api",
                "name": "Polymarket Gamma Discovery API",
                "domain": "gamma-api.polymarket.com",
                "source_type": "gamma_api",
                "endpoint_url": "https://gamma-api.polymarket.com",
                "rate_limit_rps": 5.0,
                "credibility_score": 1.0,
                "is_active": True,
                "records_observed": 0,
                "records_accepted": 0,
                "records_errored": 0,
            }
        ]

    async def record_metric(self, source_id: str, success: bool, error_msg: str = "") -> None:
        """Update source observation metrics and health."""
        if timescale_db._is_postgres and timescale_db._pool:
            try:
                async with timescale_db._pool.acquire() as conn:
                    if success:
                        await conn.execute("""
                            UPDATE raw.source_registry
                            SET records_observed = records_observed + 1,
                                records_accepted = records_accepted + 1,
                                last_success_at = NOW(),
                                updated_at = NOW()
                            WHERE source_id = $1;
                        """, source_id)
                    else:
                        await conn.execute("""
                            UPDATE raw.source_registry
                            SET records_observed = records_observed + 1,
                                records_errored = records_errored + 1,
                                last_error_at = NOW(),
                                last_error_msg = $2,
                                updated_at = NOW()
                            WHERE source_id = $1;
                        """, source_id, error_msg)
            except Exception as e:
                log.debug("[source_registry] Failed to update source metric: %s", e)


source_registry = SourceRegistry()
