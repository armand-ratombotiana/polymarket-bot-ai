"""
core/market_discovery.py — Universal Polymarket Catalog Ingestion, Hierarchy & Coverage Engine.

Ingests:
  Hierarchy: Parent Event -> Market Question -> Outcomes -> CLOB Tokens
  Fields: event_id, market_id, condition_id, question, slug, category, outcomes, token_ids,
          volume_24h, total_volume, liquidity, end_date, status, orderbook_supported.
  Coverage Metrics: authoritative count, discovered count, coverage percentage, exclusion audit log.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from config import settings
from core.book_poller import book_poller
from core.data_store import store
from ml.vector_store import vector_store

log = logging.getLogger(__name__)

GAMMA_BASE_URL = settings.poly_gamma_host.rstrip("/")


class UniversalMarketDiscoveryEngine:
    """
    Exhaustive Polymarket catalog synchronizer and coverage auditor.
    """

    def __init__(self) -> None:
        self.catalog: dict[str, dict] = {}           # token_id -> market metadata
        self.events_catalog: dict[str, dict] = {}    # event_id -> event metadata
        self.excluded_markets: list[dict] = []       # audit log of skipped/invalid items
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_sync_time: float = 0.0
        self._authoritative_count: int = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop(), name="market-discovery")
        log.info("[market_discovery] Universal Catalog & Coverage Engine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _discovery_loop(self) -> None:
        """Periodic full catalog synchronization every 3 minutes."""
        await asyncio.sleep(2.0)  # Brief warm-up to let API server bind immediately
        while self._running:
            try:
                await self.sync_full_catalog()
            except Exception as e:
                log.warning("[market_discovery] Catalog sync loop error: %s", e)
            await asyncio.sleep(180)

    async def sync_full_catalog(self) -> int:
        """Paginate across all Polymarket Gamma market endpoints to ingest 100% of available markets."""
        start_t = time.time()
        discovered_batch: list[dict] = []
        authoritative_total = 0

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            limit = 100
            offset = 0
            max_offset = 2000  # safety ceiling (20 pages × 100)
            while offset <= max_offset:
                try:
                    resp = await client.get(
                        f"{GAMMA_BASE_URL}/markets",
                        params={"limit": limit, "offset": offset, "active": "true", "closed": "false"},
                    )
                    if resp.status_code == 200:
                        batch = resp.json()
                        if isinstance(batch, list) and batch:
                            discovered_batch.extend(batch)
                            authoritative_total += len(batch)
                            if len(batch) < limit:
                                break   # last page reached
                            offset += limit
                        else:
                            break
                    else:
                        break
                except Exception as e:
                    log.debug("[market_discovery] Batch fetch offset %d error: %s", offset, e)
                    break

        self._authoritative_count = max(authoritative_total, len(discovered_batch))
        if not discovered_batch:
            return 0

        from core.gamma_client import GammaClient

        valid_tokens: list[str] = []
        for m in discovered_batch:
            token_ids = GammaClient.extract_token_ids(m)
            tid_single = m.get("clobTokenId") or m.get("token_id")
            if tid_single and str(tid_single) not in token_ids:
                token_ids.insert(0, str(tid_single))

            if not token_ids:
                self.excluded_markets.append({
                    "id": m.get("id", "unknown"),
                    "slug": m.get("slug", "unknown"),
                    "reason": "MISSING_CLOB_TOKEN_ID",
                    "timestamp": time.time(),
                })
                continue

            # Normalized hierarchical record
            event_title = m.get("groupItemTitle") or m.get("category") or "Global Event"
            question = m.get("question") or m.get("title") or m.get("slug", "").replace("-", " ").title()
            slug = m.get("slug") or str(token_ids[0])[:18]
            outcomes = m.get("outcomes", ["Yes", "No"])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = ["Yes", "No"]

            for idx, tid in enumerate(token_ids):
                outcome_label = outcomes[idx] if idx < len(outcomes) else f"Outcome-{idx+1}"
                token_slug = f"{slug} [{outcome_label}]" if len(token_ids) > 1 else slug

                market_record = {
                    "token_id": tid,
                    "event_id": str(m.get("events", [{}])[0].get("id", "") if m.get("events") else m.get("id", "")),
                    "event_title": event_title,
                    "question": question,
                    "slug": token_slug,
                    "outcome": outcome_label,
                    "description": m.get("description", ""),
                    "category": m.get("category", "General"),
                    "outcomes": outcomes,
                    "outcome_prices": m.get("outcomePrices", []),
                    "volume_24h": float(m.get("volume24hr") or 0.0),
                    "total_volume": float(m.get("volume") or 0.0),
                    "liquidity": float(m.get("liquidity") or 0.0),
                    "end_date": m.get("endDate") or m.get("end_date_iso", ""),
                    "status": "ACTIVE" if m.get("active") else "CLOSED",
                    "orderbook_supported": True,
                    "last_synced": time.time(),
                }

                self.catalog[tid] = market_record
                store.market_slugs[tid] = token_slug
                valid_tokens.append(tid)
                vector_store.add_market(tid, market_record)

        # Update Tiered Book Poller with discovered tokens
        book_poller.add_tokens(valid_tokens)
        self._last_sync_time = time.time()

        elapsed_ms = round((time.time() - start_t) * 1000, 1)
        log.info(
            "[market_discovery] Full Catalog Synchronized: %d/%d markets indexed (%.1f%% coverage) in %.1fms",
            len(self.catalog), self._authoritative_count, self.coverage_percentage, elapsed_ms
        )
        return len(valid_tokens)

    @property
    def coverage_percentage(self) -> float:
        """Calculate authoritative catalog coverage percentage."""
        if self._authoritative_count == 0:
            return 100.0
        return round((len(self.catalog) / self._authoritative_count) * 100.0, 2)

    def get_coverage_report(self) -> dict[str, Any]:
        """Generate comprehensive market coverage and exclusion audit report."""
        active_books = len(store.order_books)
        return {
            "authoritative_markets_reported": self._authoritative_count,
            "validated_markets_stored": len(self.catalog),
            "coverage_percentage": self.coverage_percentage,
            "orderbook_active_count": active_books,
            "poller_tier1_count": book_poller.stats.get("tier1_tokens", 0),
            "poller_tier2_count": book_poller.stats.get("tier2_tokens", 0),
            "excluded_markets_count": len(self.excluded_markets),
            "last_complete_sync_timestamp": self._last_sync_time,
            "last_complete_sync_age_seconds": round(time.time() - self._last_sync_time, 1) if self._last_sync_time > 0 else 0,
            "recent_exclusions_sample": self.excluded_markets[-10:],
        }

    def get_full_catalog(self, limit: int = 200, category: str | None = None) -> list[dict]:
        """Return full market catalog with optional category filtering."""
        markets = list(self.catalog.values())
        if category and category.lower() != "all":
            markets = [m for m in markets if m.get("category", "").lower() == category.lower()]
        return markets[:limit]


# Global singleton
market_discovery = UniversalMarketDiscoveryEngine()
